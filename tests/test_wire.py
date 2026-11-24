import asyncio
import json
import os
import signal
import socket
import subprocess
import sys

import pytest

from tau_acp.jsonrpc import Connection
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
        assert f"session {session} is in use by another process" in message  # τ's own refusal
        assert not gone(pid)  # the first agent's serve was left alone
        assert [r["pid"] for r in env.serve_records()] == [pid]  # and no second one is running
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


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
@pytest.mark.parametrize("starting", [True, False], ids=["while-starting", "started"])
@scripted
async def test_a_second_signal_does_not_cancel_the_shutdown(env, number, starting):
    """Ctrl-C twice (or a closed terminal's SIGHUP) still stops every serve, also one that is
    starting when the first signal comes."""
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        new = {"cwd": str(env.work), "mcpServers": []}
        if starting:
            await agent.write(request_for({"id": 9, "method": "session/new", "params": new}))
            await until(lambda: list((env.home / "sessions").glob("*")), "the start to begin")
        else:
            await agent.request("session/new", new)
        agent.process.send_signal(number)
        await asyncio.sleep(0.05)
        agent.process.send_signal(number)
        assert await asyncio.wait_for(agent.process.wait(), 60) == 0
        await until(lambda: env.serve_records() == [], "the serve to stop and remove its record")
    finally:
        await agent.stop()
        env.kill_serves()


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux's")
@scripted
async def test_a_killed_tau_acp_takes_its_serve_with_it(env):
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        pid = env.serve_records()[0]["pid"]
        agent.process.kill()  # SIGKILL: tau-acp cannot stop anything
        await agent.process.wait()
        await until(lambda: gone(pid), "the serve to get SIGTERM from the kernel", 10)
        assert env.serve_records() == []  # it wound down as on any SIGTERM
    finally:
        await agent.stop()
        env.kill_serves()


@scripted
async def test_a_non_object_prompt_block_is_invalid_params(env):
    agent = await Raw.start(env)
    try:
        await agent.request("initialize", {"protocolVersion": 1})
        _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
        session = response["result"]["sessionId"]
        for block in ("hello", 7, None, ["text"]):
            params = {"sessionId": session, "prompt": [block]}
            _, response = await agent.request("session/prompt", params)
            assert response["error"]["code"] == -32602, block
    finally:
        await agent.stop()


# what the editor sends wrongly must not stop the agent


def request_for(raw_message: dict) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **raw_message}).encode()


async def ask_permission(raw, env, answer: dict):
    """Run a prompt that asks the editor for permission, and answer that request with ``answer``
    (its ``id`` is filled in); the prompt's response."""
    await raw.request("initialize", {"protocolVersion": 1})
    _, response = await raw.request("session/new", {"cwd": str(env.work), "mcpServers": []})
    session = response["result"]["sessionId"]
    raw.last_id += 1
    waiting = raw.last_id
    line = 'call bash {"command": "echo hi"}'
    await raw.write(
        request_for(
            {"id": waiting, "method": "session/prompt", "params": prompt(session, line)},
        )
    )
    while True:
        got = await raw.read()
        if got.get("method") == "session/request_permission":
            await raw.write(request_for({"id": got["id"], **answer}))
        elif got.get("id") == waiting:
            return session, got


@scripted
async def test_a_malformed_error_response_fails_only_its_request(env):
    for bad in (
        {"code": -32000},  # no message
        "boom",  # not an object
        {"code": "x", "message": "m"},
        {"code": 1, "message": 3},
        None,
    ):
        raw = await Raw.start(env, "-e", SCRIPTED, "--set", 'permissions.ask=["bash"]')
        try:
            session, got = await ask_permission(raw, env, {"error": bad})
            assert got["result"] == {"stopReason": "end_turn"}, bad  # declined: the call failed
            assert not (env.work / "marker").exists()
            assert raw.process.returncode is None, bad  # and tau-acp is still there
            _, response = await raw.request("session/prompt", prompt(session, "again"))
            assert response["result"] == {"stopReason": "end_turn"}
        finally:
            await raw.stop()
            env.kill_serves()


@scripted
async def test_a_response_with_neither_result_nor_error_is_a_cancelled_answer(env):
    raw = await Raw.start(env, "-e", SCRIPTED, "--set", 'permissions.ask=["bash"]')
    try:
        _, got = await ask_permission(raw, env, {})
        assert got["result"] == {"stopReason": "end_turn"}
        assert raw.process.returncode is None
    finally:
        await raw.stop()
        env.kill_serves()


async def pair(limit: int, on_request):
    """A ``Connection`` with a small line limit, and the peer's end of it."""
    a, b = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=a, limit=limit)
    peer_reader, peer_writer = await asyncio.open_connection(sock=b)

    async def nobody(_method, _params):
        return None

    connection = Connection(reader, writer, on_request, nobody)
    return connection, asyncio.ensure_future(connection.run()), peer_reader, peer_writer, b


@scripted
async def test_an_overlong_line_is_answered_and_reading_goes_on(env):
    async def echo(method, _params):
        return {"method": method}

    _connection, running, peer, out, _sock = await pair(1000, echo)
    out.write(b"x" * 5000 + b"\n")  # five times the limit, then a good request
    out.write(request_for({"id": 1, "method": "first", "params": {}}) + b"\n")
    out.write(b'{"jsonrpc": "2.0", "id": 2, "method": "a", "params": {"p": "' + b"y" * 3000)
    out.write(b'"}}\n' + request_for({"id": 3, "method": "third", "params": {}}) + b"\n")
    await out.drain()
    seen = [json.loads(await asyncio.wait_for(peer.readline(), 10)) for _ in range(4)]
    assert seen[0]["error"]["code"] == -32700
    assert seen[0]["id"] is None
    # (answers are written by tasks, so their order against the second error is not fixed)
    for ident, method in ((1, "first"), (3, "third")):
        assert {"jsonrpc": "2.0", "id": ident, "result": {"method": method}} in seen
    assert [m["error"]["code"] for m in seen if "error" in m] == [-32700, -32700]  # one per line
    out.close()
    await asyncio.wait_for(running, 10)  # the end of the stream still ends it


@scripted
async def test_a_peer_that_is_gone_ends_the_connection_without_an_error(env):
    sent = asyncio.Event()

    async def late(_method, _params):
        await sent.wait()
        return {}

    _connection, running, _peer, out, sock = await pair(1 << 20, late)
    out.write(request_for({"id": 1, "method": "slow", "params": {}}) + b"\n")
    await out.drain()
    await asyncio.sleep(0.1)
    sock.shutdown(socket.SHUT_RD)  # the peer stops reading, but its end stays open: no EOF
    sent.set()  # the answer cannot be written
    await asyncio.wait_for(running, 10)  # run() returns, though the stream has not ended


@scripted
async def test_an_editor_that_stops_reading_ends_tau_acp_cleanly(env):
    """stdout closed while tau-acp writes: the editor is gone, which is not a serve exiting."""
    env.configure(delay_ms=300, reply="one two three four five six seven eight")
    process = subprocess.Popen(
        [sys.executable, "-m", "tau_acp"],
        cwd=env.work,
        env={**os.environ, **env.environ},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def send(message: dict) -> None:
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        process.stdin.flush()

    def receive() -> dict:
        return json.loads(process.stdout.readline())

    try:
        send({"id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        assert receive()["id"] == 1
        send({"id": 2, "method": "session/new", "params": {"cwd": str(env.work), "mcpServers": []}})
        while "result" not in (got := receive()):
            pass
        send(
            {
                "id": 3,
                "method": "session/prompt",
                "params": prompt(got["result"]["sessionId"], "go"),
            }
        )
        while receive().get("method") != "session/update":
            pass
        process.stdout.close()  # the editor stops reading; stdin stays open
        code = await asyncio.get_running_loop().run_in_executor(None, process.wait, 40)
        assert code == 0
        error = process.stderr.read().decode()
        assert "tau serve for session" not in error
        assert "the editor is gone" in error
        assert env.serve_records() == []  # its sessions are closed
    finally:
        process.kill()
        process.wait()
        for pipe in (process.stdin, process.stderr):
            pipe.close()


@scripted
async def test_a_bad_line_to_an_editor_that_is_gone_exits_zero(env):
    """Answering a malformed line is how tau-acp finds the editor gone: a clean exit, as when
    any other write finds it."""
    process = subprocess.Popen(
        [sys.executable, "-m", "tau_acp"],
        cwd=env.work,
        env={**os.environ, **env.environ},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdout.close()  # the editor stops reading
        process.stdin.write(b"not json\n")
        process.stdin.flush()
        code = await asyncio.get_running_loop().run_in_executor(None, process.wait, 40)
        error = process.stderr.read().decode()
        assert code == 0, error
        assert "Traceback" not in error
        assert "the editor is gone" in error
    finally:
        process.kill()
        process.wait()
        for pipe in (process.stdin, process.stderr):
            pipe.close()
