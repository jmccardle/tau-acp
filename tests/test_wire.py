import asyncio
import json

from tests.harness import SCRIPTED, Raw, gone, scripted, until

PROMPT = {"type": "text"}


def prompt(session: str, text: str) -> dict:
    return {"sessionId": session, "prompt": [{"type": "text", "text": text}]}


@scripted
async def test_initialize_on_the_wire(env):
    agent = await Raw.start(env)
    try:
        before, response = await agent.request(
            "initialize", {"protocolVersion": 7, "clientCapabilities": {}}
        )
        assert before == []
        result = response["result"]
        assert result["protocolVersion"] == 1  # whatever the client asked for
        assert result["authMethods"] == []
        assert result["agentInfo"]["name"] == "tau-acp"
        caps = result["agentCapabilities"]
        assert caps["loadSession"] is True
        assert caps["promptCapabilities"] == {
            "image": False,
            "audio": False,
            "embeddedContext": True,
        }
        assert caps["mcpCapabilities"] == {"http": False, "sse": False}
        assert caps["sessionCapabilities"] == {"list": {}, "resume": {}, "close": {}}
    finally:
        await agent.stop()


@scripted
async def test_protocol_errors(env):
    agent = await Raw.start(env)
    try:
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        assert response["error"]["code"] == -32600  # before initialize
        await agent.request("initialize", {"protocolVersion": 1})
        _, response = await agent.request("no/such_method", {})
        assert response["error"]["code"] == -32601
        await agent.write(b"{not json")
        garbled = await agent.read()
        assert garbled["id"] is None
        assert garbled["error"]["code"] == -32700
        await agent.write(b"[1, 2]")
        assert (await agent.read())["error"]["code"] == -32600
        # notifications nobody listens to get no answer, and do not stop the agent
        await agent.notify("session/cancel", {"sessionId": "nope"})
        await agent.notify("$/cancel_request", {"requestId": 1})
        await agent.notify("_vendor/thing", {})
        _, response = await agent.request("session/prompt", prompt("nope", "hi"))
        assert response["error"]["code"] == -32002
        assert response["error"]["data"] == {"sessionId": "nope"}
    finally:
        await agent.stop()


@scripted
async def test_load_replays_before_it_responds(env):
    first = await Raw.start(env, "-e", SCRIPTED)
    try:
        await first.request("initialize", {"protocolVersion": 1})
        _, response = await first.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        command = 'call bash {"command": "echo replayed"}'
        before, response = await first.request("session/prompt", prompt(session, command))
        assert response["result"] == {"stopReason": "end_turn"}
        assert before[0]["method"] == "session/update"  # the commands came after new's response
        assert await first.eof() == 0
    finally:
        await first.stop()
    assert gone_all(env)

    second = await Raw.start(env, "-e", SCRIPTED)
    try:
        await second.request("initialize", {"protocolVersion": 1})
        params = {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        before, response = await second.request("session/load", params)
        assert list(response["result"]) == ["configOptions"]
        updates = [m["params"]["update"] for m in before]
        assert all(m["method"] == "session/update" for m in before)
        assert [u["sessionUpdate"] for u in updates] == [
            "user_message_chunk",
            "agent_message_chunk",  # "calling"
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",  # the mock's reply after the result
        ]
        assert updates[0]["content"]["text"] == command
        assert updates[1]["content"]["text"] == "calling"
        assert updates[2]["status"] == "pending"
        assert updates[3]["status"] == "completed"
        assert updates[3]["toolCallId"] == updates[2]["toolCallId"]
        assert "replayed" in updates[3]["content"][0]["content"]["text"]
        # the command list follows the response
        commands = await second.read()
        assert commands["params"]["update"]["sessionUpdate"] == "available_commands_update"
        assert commands["params"]["sessionId"] == session
    finally:
        await second.stop()


def gone_all(env) -> bool:
    return all(gone(r["pid"]) for r in env.serve_records())


@scripted
async def test_load_errors(env):
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        params = {"sessionId": "20000101-000000-abcdef", "cwd": str(env.work), "mcpServers": []}
        _, response = await agent.request("session/load", params)
        assert response["error"]["code"] == -32002
        assert response["error"]["data"] == {"sessionId": "20000101-000000-abcdef"}
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        other = env.home / "elsewhere"
        other.mkdir()
        _, response = await agent.request(
            "session/load", {"sessionId": session, "cwd": str(other), "mcpServers": []}
        )
        assert response["error"]["code"] == -32602
        assert str(env.work) in response["error"]["message"]
    finally:
        await agent.stop()


@scripted
async def test_a_second_prompt_is_refused(env):
    env.configure(delay_ms=300, reply="one two three four five six")
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        await agent.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 100,
                    "method": "session/prompt",
                    "params": prompt(session, "a"),
                }
            ).encode()
        )
        # a second one while the first runs: refused at once; the first still ends its turn
        second = await agent.request("session/prompt", prompt(session, "b"))
        assert second[1]["error"]["code"] == -32602
        assert "already running" in second[1]["error"]["message"]
        await agent.notify("session/cancel", {"sessionId": session})
        while (message := await agent.read()).get("id") != 100:
            pass
        assert message["result"] == {"stopReason": "cancelled"}
    finally:
        await agent.stop()


@scripted
async def test_session_served_elsewhere_is_not_attached(env):
    agent = await Raw.start(env)
    other = await Raw.start(env)
    try:
        for each in (agent, other):
            await each.request("initialize", {"protocolVersion": 1})
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        pid = env.serve_records()[0]["pid"]
        params = {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        _, response = await other.request("session/load", params)
        message = response["error"]["message"]
        assert response["error"]["code"] == -32603
        assert str(pid) in message
        assert f"tau serve --stop --session {session}" in message
        assert not gone(pid)  # the first agent's serve was left alone
    finally:
        await agent.stop()
        await other.stop()


@scripted
async def test_stdin_eof_stops_every_serve_and_exits_zero(env):
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        for _ in range(2):
            await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        pids = [r["pid"] for r in env.serve_records()]
        assert len(pids) == 2
        assert not any(gone(pid) for pid in pids)
        assert await agent.eof() == 0
        await until(lambda: all(gone(pid) for pid in pids), "the serves to stop", 10)
        assert env.serve_records() == []
    finally:
        await agent.stop()


@scripted
async def test_eof_in_the_middle_of_a_turn(env):
    env.configure(delay_ms=500, reply="one two three four five six seven eight")
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        pid = env.serve_records()[0]["pid"]
        await agent.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 100,
                    "method": "session/prompt",
                    "params": prompt(session, "a"),
                }
            ).encode()
        )
        while (await agent.read()).get("method") != "session/update":
            pass
        assert await agent.eof() == 0
        await until(lambda: gone(pid), "the serve to stop", 10)
    finally:
        await agent.stop()


@scripted
async def test_sigterm_stops_the_serves_too(env):
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        pid = env.serve_records()[0]["pid"]
        agent.process.terminate()
        assert await asyncio.wait_for(agent.process.wait(), 60) == 0
        await until(lambda: gone(pid), "the serve to stop", 10)
    finally:
        await agent.stop()
