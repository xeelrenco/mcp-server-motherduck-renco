"""Accesso al server MCP in modalità HTTP: OAuth Google (data store di Gemini Enterprise) e chiave segreta.

Entrambi sono facoltativi e si attivano con variabili d'ambiente:
  MCP_OAUTH_CLIENT_ID  ID del client OAuth con cui Gemini Enterprise fa accedere l'utente.
                       Con questa variabile ogni richiesta deve portare un token di accesso Google
                       ("Authorization: Bearer ...") emesso per quel client, con email verificata.
  MCP_API_KEY          Chiave segreta per i client che non fanno OAuth (es. l'agente su Vertex),
                       accettata in "X-API-Key: ..." oppure "Authorization: Bearer ...".
Se nessuna delle due è impostata il server resta aperto, come prima.
Se sono impostate entrambe, basta una delle due.
"""

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request

import anyio
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

logger = logging.getLogger("mcp_server_motherduck")

_token_cache: dict = {}


def _tokeninfo(token: str):
    url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(token, safe="")
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # token scaduto o non valido: Google risponde 400
        return None


async def _verify_google_token(token: str, client_id: str) -> bool:
    key = hashlib.sha256(token.encode()).hexdigest()
    cached = _token_cache.get(key)
    if cached and cached > time.time():
        return True
    info = await anyio.to_thread.run_sync(_tokeninfo, token)
    if not info:
        return False
    if client_id not in (info.get("aud"), info.get("azp")):
        logger.warning("Token Google emesso per un altro client OAuth: rifiutato")
        return False
    if str(info.get("email_verified")).lower() != "true" or not info.get("email"):
        return False
    _token_cache[key] = time.time() + min(int(info.get("expires_in") or 0), 300)
    if len(_token_cache) > 500:
        _token_cache.clear()
    return True


class AccessMiddleware:
    def __init__(self, app, client_id: str = "", api_key: str = "") -> None:
        self.app = app
        self.client_id = client_id
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        bearer = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            bearer = auth[7:].strip()
        if self.api_key:
            for candidate in (headers.get("x-api-key", ""), bearer):
                if candidate and hmac.compare_digest(candidate, self.api_key):
                    return await self.app(scope, receive, send)
        if self.client_id and bearer and await _verify_google_token(bearer, self.client_id):
            return await self.app(scope, receive, send)
        response = JSONResponse(
            {"error": "Accesso non autorizzato"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        return await response(scope, receive, send)


def http_middleware() -> list:
    """Middleware da passare a mcp.run(); lista vuota se l'accesso non è configurato."""
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
    api_key = os.environ.get("MCP_API_KEY", "").strip()
    if not client_id and not api_key:
        logger.warning("Accesso HTTP aperto: nessuna autenticazione configurata (MCP_OAUTH_CLIENT_ID / MCP_API_KEY)")
        return []
    methods = [m for m, v in (("OAuth Google", client_id), ("chiave segreta", api_key)) if v]
    logger.info("Accesso HTTP protetto: %s", " + ".join(methods))
    return [Middleware(AccessMiddleware, client_id=client_id, api_key=api_key)]
