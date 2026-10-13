"""Accesso al server MCP in modalità HTTP: OAuth 2.0 integrato (per il data store di Gemini Enterprise) e chiave segreta.

Il data store di Gemini accetta solo "nessuna autenticazione" oppure OAuth 2.0. Qui l'OAuth è fatto dal server
stesso, senza Google come intermediario: ID e segreto del client li scegli tu e li inserisci sia su Render sia
in Gemini. Chi non ha il segreto non può ottenere un token, quindi non può usare il server.
Gli utenti abilitati restano quelli di Gemini Enterprise: il server non ha un proprio elenco utenti.

Variabili d'ambiente (tutte facoltative; senza nessuna il server resta aperto, come prima):
  MCP_OAUTH_CLIENT_ID      ID del client, scelto da te (es. "gemini-mdr")
  MCP_OAUTH_CLIENT_SECRET  segreto lungo e casuale (es. 48 caratteri). Servono entrambi.
  MCP_OAUTH_REDIRECT_URIS  facoltativa: indirizzi di ritorno ammessi, separati da virgola (quello mostrato da Gemini).
                           Se vuota è accettato qualsiasi indirizzo https.
  MCP_API_KEY              chiave per client che non fanno OAuth (es. l'agente su Vertex): "X-API-Key" o "Bearer".
Se sono attivi entrambi i metodi basta uno dei due.

Endpoint OAuth esposti: /authorize e /token (i token sono firmati, senza archivio: sopravvivono ai riavvii).
Cambiare il segreto invalida tutti i token emessi.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger("mcp_server_motherduck")

CODE_TTL = 120
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret: str) -> None:
        self.key = hashlib.sha256(("mdr-mcp|" + secret).encode()).digest()

    def make(self, kind: str, ttl: int, **claims) -> str:
        payload = _b64(json.dumps({"k": kind, "exp": int(time.time()) + ttl, "n": secrets.token_hex(4), **claims}).encode())
        sig = _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def read(self, token: str, kind: str):
        try:
            payload, sig = token.split(".", 1)
            good = _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                return None
            data = json.loads(_unb64(payload))
        except Exception:  # noqa: BLE001
            return None
        if data.get("k") != kind or data.get("exp", 0) < time.time():
            return None
        return data


def _oauth_error(error: str, description: str = "", status: int = 400) -> Response:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


class AccessMiddleware:
    def __init__(self, app, client_id: str = "", client_secret: str = "", redirect_uris=(), api_key: str = "") -> None:
        self.app = app
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uris = [u for u in redirect_uris if u]
        self.api_key = api_key
        self.signer = Signer(client_secret) if client_id and client_secret else None

    # -- endpoint OAuth ---------------------------------------------------------
    def _redirect_ok(self, uri: str) -> bool:
        if self.redirect_uris:
            return uri in self.redirect_uris
        return urlparse(uri).scheme == "https"

    async def _authorize(self, request: Request) -> Response:
        q = request.query_params
        if q.get("client_id") != self.client_id:
            return _oauth_error("invalid_client", "client_id non valido", 401)
        redirect_uri = q.get("redirect_uri", "")
        if not self._redirect_ok(redirect_uri):
            logger.warning("OAuth: indirizzo di ritorno non ammesso: %s", redirect_uri)
            return _oauth_error("invalid_request", "redirect_uri non ammesso")
        if q.get("response_type") != "code":
            return _oauth_error("unsupported_response_type")
        # Nessuna schermata di accesso: la protezione è il segreto del client, richiesto allo scambio del codice.
        code = self.signer.make(
            "code", CODE_TTL, cid=self.client_id, ru=redirect_uri,
            cc=q.get("code_challenge", ""), cm=q.get("code_challenge_method", "plain"),
        )
        params = {"code": code}
        if q.get("state"):
            params["state"] = q["state"]
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)

    def _client_credentials(self, request: Request, form: dict):
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:  # noqa: BLE001
                return False
        return hmac.compare_digest(cid, self.client_id) and hmac.compare_digest(secret, self.client_secret)

    def _tokens(self, with_refresh: bool) -> dict:
        out = {"access_token": self.signer.make("access", ACCESS_TTL), "token_type": "Bearer", "expires_in": ACCESS_TTL}
        if with_refresh:
            out["refresh_token"] = self.signer.make("refresh", REFRESH_TTL)
        return out

    async def _token(self, request: Request) -> Response:
        if request.method != "POST":
            return _oauth_error("invalid_request", "serve POST", 405)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        if not self._client_credentials(request, form):
            return _oauth_error("invalid_client", "client_id o segreto non validi", 401)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            data = self.signer.read(form.get("code", ""), "code")
            if not data or data.get("cid") != self.client_id:
                return _oauth_error("invalid_grant", "codice non valido o scaduto")
            if data.get("ru") != form.get("redirect_uri", data.get("ru")):
                return _oauth_error("invalid_grant", "redirect_uri diverso da quello della richiesta")
            if data.get("cc"):
                verifier = form.get("code_verifier", "")
                expected = data["cc"]
                got = _b64(hashlib.sha256(verifier.encode()).digest()) if data.get("cm") == "S256" else verifier
                if not hmac.compare_digest(got, expected):
                    return _oauth_error("invalid_grant", "code_verifier errato")
            return JSONResponse(self._tokens(True))
        if grant == "refresh_token":
            if not self.signer.read(form.get("refresh_token", ""), "refresh"):
                return _oauth_error("invalid_grant", "refresh token non valido o scaduto")
            return JSONResponse(self._tokens(True))
        if grant == "client_credentials":
            return JSONResponse(self._tokens(False))
        return _oauth_error("unsupported_grant_type")

    # -- controllo di ogni richiesta -----------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.signer and scope["path"] in ("/authorize", "/token"):
            request = Request(scope, receive)
            handler = self._authorize if scope["path"] == "/authorize" else self._token
            return await (await handler(request))(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if self.api_key:
            for candidate in (headers.get("x-api-key", ""), bearer):
                if candidate and hmac.compare_digest(candidate, self.api_key):
                    return await self.app(scope, receive, send)
        if self.signer and bearer and self.signer.read(bearer, "access"):
            return await self.app(scope, receive, send)
        response = JSONResponse(
            {"error": "Accesso non autorizzato"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        return await response(scope, receive, send)


def http_middleware() -> list:
    """Middleware da passare a mcp.run(); lista vuota se l'accesso non è configurato."""
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "").strip()
    api_key = os.environ.get("MCP_API_KEY", "").strip()
    redirects = [u.strip() for u in os.environ.get("MCP_OAUTH_REDIRECT_URIS", "").split(",")]
    if bool(client_id) != bool(client_secret):
        raise SystemExit("MCP_OAUTH_CLIENT_ID e MCP_OAUTH_CLIENT_SECRET vanno impostati insieme")
    if client_secret and len(client_secret) < 24:
        raise SystemExit("MCP_OAUTH_CLIENT_SECRET troppo corto: usa almeno 24 caratteri casuali")
    if not client_id and not api_key:
        logger.warning("Accesso HTTP aperto: nessuna autenticazione configurata")
        return []
    methods = [m for m, v in (("OAuth", client_id), ("chiave segreta", api_key)) if v]
    logger.info("Accesso HTTP protetto: %s", " + ".join(methods))
    return [Middleware(AccessMiddleware, client_id=client_id, client_secret=client_secret,
                       redirect_uris=redirects, api_key=api_key)]
