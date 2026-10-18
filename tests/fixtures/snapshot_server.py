"""Tiny real stdio MCP peer for the local snapshot-client test."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1.0.0"}}
    elif request["method"] == "tools/list":
        result = {"tools": [{"name": "read_doc", "description": "Read a document", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
