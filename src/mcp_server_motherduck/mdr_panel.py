"""
Pannello MDR (prova): interfaccia MCP Apps mostrata dentro la chat.

Serve a verificare cosa consente il client (Gemini Enterprise) a un pannello
interattivo: collegamento, chiamate al server, scelta file, rete, link, messaggi.
Attivo solo se la variabile d'ambiente MDR_PANEL_ENABLED vale "true".
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
import mcp.types as mcp_types

logger = logging.getLogger("mcp_server_motherduck")

PANEL_URI = "ui://renco-mdr/pannello-prova"
PANEL_MIME = "text/html;profile=mcp-app"
PANEL_HTML = Path(__file__).parent / "assets" / "pannello_mdr.html"
PANEL_UI_META = {
    "ui": {
        "prefersBorder": True,
        "csp": {
            # Domini che il pannello può chiamare (prova di caricamento diretto nel bucket)
            "connectDomains": ["https://storage.googleapis.com"],
            "resourceDomains": [],
        },
    }
}


def panel_enabled() -> bool:
    return os.environ.get("MDR_PANEL_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _inject_resource_meta(mcp: FastMCP) -> None:
    """FastMCP 2.x non riporta _meta nel contenuto letto della risorsa: lo aggiungiamo qui.

    I client MCP Apps leggono csp e prefersBorder dal contenuto della risorsa.
    """
    handlers = mcp._mcp_server.request_handlers
    original = handlers[mcp_types.ReadResourceRequest]

    async def handler(req: mcp_types.ReadResourceRequest):
        result = await original(req)
        if str(req.params.uri) == PANEL_URI and isinstance(result.root, mcp_types.ReadResourceResult):
            for item in result.root.contents:
                item.meta = PANEL_UI_META
        return result

    handlers[mcp_types.ReadResourceRequest] = handler


def register_mdr_panel(mcp: FastMCP, db_client) -> None:
    if not panel_enabled():
        return
    if not PANEL_HTML.exists():
        logger.warning("Pannello MDR: file %s mancante, pannello non registrato", PANEL_HTML)
        return

    _inject_resource_meta(mcp)

    @mcp.resource(
        PANEL_URI,
        name="pannello_mdr_prova",
        title="Pannello MDR (prova)",
        description="Pannello interattivo di prova del generatore MDR",
        mime_type=PANEL_MIME,
        meta=PANEL_UI_META,
    )
    def pannello_mdr_html() -> str:
        return PANEL_HTML.read_text(encoding="utf-8")

    @mcp.tool(
        name="apri_pannello_mdr",
        title="Apri pannello MDR (prova)",
        description=(
            "Mostra all'utente il pannello interattivo di prova del generatore MDR. "
            "Usalo quando l'utente chiede di aprire il pannello MDR o di provare il pannello. "
            "Dopo la chiamata, di' all'utente che il pannello è caricato e di seguire i passi indicati."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        meta={"ui": {"resourceUri": PANEL_URI, "visibility": ["model", "app"]}},
    )
    def apri_pannello_mdr() -> dict:
        return {
            "messaggio": "Pannello MDR di prova caricato.",
            "ora_server": _now(),
            "app_url": os.environ.get("MDR_APP_URL", ""),
        }

    @mcp.tool(
        name="pannello_mdr_ping",
        title="Verifica pannello MDR",
        description="Usato solo dal pannello MDR per verificare il collegamento con il server.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        meta={"ui": {"resourceUri": PANEL_URI, "visibility": ["app"]}},
    )
    def pannello_mdr_ping() -> dict:
        righe = None
        try:
            _, _, rows = db_client.execute_raw(
                "SELECT count(*) FROM my_db.historical_mdr_normalization.v_MdrPreviousRecords_Normalized_All"
            )
            righe = rows[0][0] if rows else None
        except Exception as exc:  # il ping non deve fallire per un problema di database
            righe = f"non disponibile ({exc.__class__.__name__})"
        return {"ora_server": _now(), "righe_mdr_storiche": righe}

    logger.info("Pannello MDR di prova registrato (%s)", PANEL_URI)
