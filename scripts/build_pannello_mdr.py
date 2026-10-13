"""Genera assets/pannello_mdr.html e assets/generatore_mdr.html inserendo la libreria MCP Apps nel template.

Uso (dalla cartella del progetto):
    npm install --prefix build-ui @modelcontextprotocol/ext-apps@2.0.3
    python scripts/build_pannello_mdr.py build-ui/node_modules/@modelcontextprotocol/ext-apps
"""

import json
import re
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "src" / "mcp_server_motherduck" / "assets"


def main(sdk_dir: str) -> None:
    sdk = Path(sdk_dir)
    version = json.loads((sdk / "package.json").read_text(encoding="utf-8"))["version"]
    bundle = (sdk / "dist" / "src" / "app-with-deps.js").read_text(encoding="utf-8")
    m = list(re.finditer(r"export\s*\{([^}]*)\}\s*;?\s*$", bundle))
    if len(m) != 1:
        raise SystemExit("Formato della libreria MCP Apps non riconosciuto")
    names = {}
    for part in m[0].group(1).split(","):
        local, _, exported = part.strip().partition(" as ")
        names[exported or local] = local
    js = "(function(){" + bundle[: m[0].start()] + "\nwindow.__mcpApps={App:%s};})();" % names["App"]
    if "</script" in js.lower():
        raise SystemExit("La libreria contiene </script>: non si può inserire nella pagina")
    for name in ("pannello_mdr", "generatore_mdr"):
        template = (ASSETS / f"{name}.template.html").read_text(encoding="utf-8")
        out = template.replace("/*__MCP_APPS_SDK__*/", f"/* @modelcontextprotocol/ext-apps {version} (MIT) */\n" + js)
        (ASSETS / f"{name}.html").write_text(out, encoding="utf-8", newline="\n")
        print(f"Scritto {ASSETS / (name + '.html')} ({len(out)} caratteri, ext-apps {version})")


if __name__ == "__main__":
    main(sys.argv[1])
