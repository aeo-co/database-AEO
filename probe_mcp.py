import json, urllib.request

BASE = "http://147.182.215.4:8765/mcp"
HDRS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def post(payload):
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=HDRS)
    r = urllib.request.urlopen(req, timeout=15)
    body = r.read().decode()
    sid = r.headers.get("Mcp-Session-Id")
    return body, sid

init = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
    "protocolVersion":"2024-11-05","capabilities":{},
    "clientInfo":{"name":"probe","version":"0.1"}}}
body, sid = post(init)
print("INIT:", body[:200])
if sid:
    HDRS["Mcp-Session-Id"] = sid
    try:
        post({"jsonrpc":"2.0","method":"notifications/initialized"})
    except Exception:
        pass
body, _ = post({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
data = json.loads(body)
names = [t["name"] for t in data["result"]["tools"]]
print("TOOLS:", names)
print("HAS_GET_CLIENT_CONTEXT:", "get_client_context" in names)
print("HAS_INGEST_CLIENT_CONTEXT:", "ingest_client_context" in names)
