"""A small stdio MCP server for the tests (standard library only).

Environment: MCP_LOG, a file it appends one JSON line to for each message it receives and for
its start (``{"started": pid}``); PAGE_SIZE, to page ``tools/list`` over extra tools
``tool_0`` .. ``tool_4``; TEST_SECRET, which the ``env`` tool returns.
"""

import json
import os
import sys
import threading

LOG = os.environ["MCP_LOG"]
PAGE = int(os.environ.get("PAGE_SIZE", "0"))
WRITE = threading.Lock()
CANCELLED: dict[int, threading.Event] = {}


def log(entry):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def send(message):
    with WRITE:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def tool(name, **properties):
    schema = {"type": "object", "properties": {k: {"type": v} for k, v in properties.items()}}
    return {"name": name, "description": f"the {name} tool", "inputSchema": schema}


TOOLS = [
    tool("echo", text="string"),
    tool("fail"),
    tool("slow", seconds="number"),
    tool("image"),
    tool("env"),
    tool("die"),
    tool("structured"),
    *([tool(f"tool_{n}") for n in range(5)] if PAGE else []),
]


def text(value, error=False):
    return {"content": [{"type": "text", "text": value}], "isError": error}


def slow(ident, seconds):
    event = CANCELLED[ident] = threading.Event()
    if event.wait(seconds):
        return  # cancelled: a server sends no result for a cancelled request
    send({"jsonrpc": "2.0", "id": ident, "result": text("slept")})


IMAGE = {"type": "image", "data": "AAAA", "mimeType": "image/png"}


def call(ident, name, args):
    if name == "die":
        os._exit(3)
    if name == "slow":
        threading.Thread(target=slow, args=(ident, args["seconds"]), daemon=True).start()
        return None
    results = {
        "echo": lambda: text(args.get("text", "")),
        "fail": lambda: text("it failed", error=True),
        "image": lambda: {"content": [{"type": "text", "text": "before"}, IMAGE]},
        "env": lambda: text(os.environ.get("TEST_SECRET", "unset")),
        "structured": lambda: {"content": [], "structuredContent": {"answer": 42}},
    }
    return results.get(name, lambda: text(f"called {name}"))()


def tools_list(params):
    start = int(params.get("cursor", 0))
    if not PAGE:
        return {"tools": TOOLS}
    page = {"tools": TOOLS[start : start + PAGE]}
    if start + PAGE < len(TOOLS):
        page["nextCursor"] = str(start + PAGE)
    return page


def handle(message):
    method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test", "version": "1"},
        }
    elif method == "tools/list":
        result = tools_list(params)
    elif method == "tools/call":
        result = call(ident, params["name"], params.get("arguments") or {})
        if result is None:
            return
    elif method == "notifications/cancelled":
        CANCELLED[params["requestId"]].set()
        return
    elif ident is None:
        return
    else:
        send({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no"}})
        return
    send({"jsonrpc": "2.0", "id": ident, "result": result})


log({"started": os.getpid(), "cwd": os.getcwd()})
for line in sys.stdin:
    message = json.loads(line)
    log(
        {
            "method": message.get("method"),
            "params": message.get("params") or {},
            "id": message.get("id"),
        }
    )
    handle(message)
