import sys
U="/mnt/user-data/uploads/Server MCP/src/mcp_server_motherduck/assets/"
V="/tmp/claude-0/-home-claude/99325d8a-6114-5328-8085-b0dfacb80859/scratchpad/pdfjs/node_modules/pdfjs-dist/build/"
tpl=open(U+"generatore_mdr.template.html",encoding="utf-8",newline="").read().replace("\r\n","\n")
old=open(U+"generatore_mdr.html",encoding="utf-8",newline="").read()
i=old.index("<script>/* @modelcontextprotocol"); j=old.index("</script>",i)+9
lib=open(V+"pdf.min.js",encoding="utf-8").read(); wk=open(V+"pdf.worker.min.js",encoding="utf-8").read()
for s in (lib,wk): assert "</script" not in s and "<!--" not in s
out=tpl.replace("<script>/*__MCP_APPS_SDK__*/</script>",old[i:j],1).replace("/*__PDFJS_LIB__*/",lib,1).replace("/*__PDFJS_WORKER__*/",wk,1)
open(sys.argv[1],"w",encoding="utf-8",newline="").write(out); print(len(out))
