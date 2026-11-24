import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from acp import RequestError

from tests.harness import (
    SCRIPTED,
    Raw,
    agent,
    gone,
    mcp_log,
    mcp_pid,
    mcp_server,
    scripted,
    text,
    until,
)

OMITTED = "[image content omitted: τ tool results are text]"


def call(tool: str, **args) -> str:
    return f"call {tool} {json.dumps(args)}"


async def run(conn, env, tool, *servers, **args):
    session = await conn.new_session(cwd=str(env.work), mcp_servers=list(servers))
    await conn.prompt(session_id=session.session_id, prompt=[text(call(tool, **args))])
    return session


@scripted
async def test_mcp_tool_reaches_the_model_and_back(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__echo", mcp_server(env), text="hello mcp")
        (started,) = editor.of("tool_call")
        (done,) = editor.of("tool_call_update")
        assert started.kind == "other"
        assert "mcp__test__echo" in started.title
        assert started.raw_input == {"text": "hello mcp"}
        assert done.status == "completed"
        assert done.content[0].content.text == "hello mcp"
        # the server ran in the session's cwd
        assert mcp_log(env)[0]["cwd"] == str(env.work)


@scripted
async def test_is_error_fails_the_call(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__fail", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert done.status == "failed"
        assert done.content[0].content.text == "it failed"


@scripted
async def test_image_block_is_omitted_with_a_line(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__image", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert done.status == "completed"
        assert done.content[0].content.text == f"before\n{OMITTED}"


@scripted
async def test_structured_content_only_is_its_json(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__structured", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert json.loads(done.content[0].content.text) == {"answer": 42}


@scripted
async def test_cancel_during_a_slow_call_cancels_the_mcp_request(env):
    async with agent(env, "-e", SCRIPTED) as (conn, _editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[mcp_server(env)])
        slow = call("mcp__test__slow", seconds=60)
        prompt = asyncio.create_task(
            conn.prompt(session_id=session.session_id, prompt=[text(slow)])
        )
        await until(
            lambda: any(e.get("params", {}).get("name") == "slow" for e in mcp_log(env)),
            "the slow call to reach the MCP server",
        )
        await conn.cancel(session_id=session.session_id)
        assert (await prompt).stop_reason == "cancelled"
        await until(
            lambda: any(e.get("method") == "notifications/cancelled" for e in mcp_log(env)),
            "notifications/cancelled",
        )
        (cancelled,) = (e for e in mcp_log(env) if e.get("method") == "notifications/cancelled")
        call_id = next(e["id"] for e in mcp_log(env) if e.get("method") == "tools/call")
        assert cancelled["params"]["requestId"] == call_id


@scripted
async def test_paginated_tools_list(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__tool_4", mcp_server(env, PAGE_SIZE="3"))
        (done,) = editor.of("tool_call_update")
        assert done.content[0].content.text == "called tool_4"
        lists = [e for e in mcp_log(env) if e.get("method") == "tools/list"]
        assert len(lists) == 6  # 17 tools in pages of 3
        assert lists[1]["params"] == {"cursor": "3"}


@scripted
async def test_env_is_passed_to_the_server(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__env", mcp_server(env, TEST_SECRET="s3cret-value"))
        (done,) = editor.of("tool_call_update")
        assert done.content[0].content.text == "s3cret-value"


@scripted
async def test_bad_command_names_the_server_and_leaves_no_serve(env):
    broken = mcp_server(env, "broken", TEST_SECRET="hunter2")
    broken.command = str(env.work / "no-such-command")
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.new_session(cwd=str(env.work), mcp_servers=[broken])
        assert caught.value.code == -32603
        assert "MCP server broken" in str(caught.value)
        assert "hunter2" not in str(caught.value)
        assert env.serve_records() == []


@scripted
async def test_a_server_that_fails_its_handshake_is_stopped(env):
    failing = mcp_server(env, "mute")
    failing.args = ["-c", "import sys; sys.exit(7)"]
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.new_session(cwd=str(env.work), mcp_servers=[failing])
        assert "MCP server mute exited" in str(caught.value)
        assert env.serve_records() == []


@scripted
async def test_later_server_failing_stops_the_earlier_one(env):
    broken = mcp_server(env, "broken")
    broken.command = str(env.work / "no-such-command")
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError):
            await conn.new_session(cwd=str(env.work), mcp_servers=[mcp_server(env), broken])
        await until(lambda: gone(mcp_pid(env)), "the first MCP server to be stopped")


@scripted
async def test_serve_failure_stops_the_mcp_servers(env):
    async with agent(env, "--no-such-serve-flag") as (conn, _editor, _process):
        with pytest.raises(RequestError):
            await conn.new_session(cwd=str(env.work), mcp_servers=[mcp_server(env)])
        await until(lambda: gone(mcp_pid(env)), "the MCP server to be stopped")
        assert list(env.runtime.iterdir()) == []


@scripted
async def test_names_that_collide_or_are_too_long(env):
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.new_session(
                cwd=str(env.work),
                mcp_servers=[mcp_server(env, "a.b"), mcp_server(env, "a_b")],
            )
        assert caught.value.code == -32602
        assert "mcp__a_b__echo" in str(caught.value)
        long = "s" * 60
        with pytest.raises(RequestError) as caught:
            await conn.new_session(cwd=str(env.work), mcp_servers=[mcp_server(env, long)])
        assert caught.value.code == -32602
        assert long in str(caught.value)
        assert env.serve_records() == []
        await until(lambda: gone(mcp_pid(env, long)), "the MCP server to be stopped")


@scripted
async def test_http_and_sse_are_refused(env):
    # raw lines: the SDK's client drops http/sse entries it was not told the agent supports
    raw = await Raw.start(env)
    try:
        await raw.request("initialize", {"protocolVersion": 1})
        for kind in ("http", "sse"):
            server = {"type": kind, "name": "web", "url": "http://localhost:1/x", "headers": []}
            _, reply = await raw.request(
                "session/new", {"cwd": str(env.work), "mcpServers": [server]}
            )
            assert reply["error"]["code"] == -32602
            assert "stdio MCP servers only" in reply["error"]["message"]
            assert "web" in reply["error"]["message"]
        assert env.serve_records() == []
    finally:
        await raw.stop()


@scripted
async def test_session_load_restarts_the_mcp_servers(env):
    async with agent(env, "-e", SCRIPTED) as (conn, _editor, _process):
        session = await run(conn, env, "mcp__test__echo", mcp_server(env), text="first")
        first = mcp_pid(env)
    await until(lambda: gone(first), "the first MCP server to stop")
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await conn.load_session(
            cwd=str(env.work), session_id=session.session_id, mcp_servers=[mcp_server(env)]
        )
        editor.forget()
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("mcp__test__echo", text="again"))]
        )
        (done,) = editor.of("tool_call_update")
        assert done.content[0].content.text == "again"
        assert mcp_pid(env) == first  # the first start is the first log entry
        starts = [e["started"] for e in mcp_log(env) if "started" in e]
        assert len(starts) == 2


@scripted
async def test_eof_stops_the_mcp_server_and_removes_the_relay(env):
    raw = await Raw.start(env, "-e", SCRIPTED)
    try:
        await raw.request("initialize", {"protocolVersion": 1})
        _, new = await raw.request(
            "session/new", {"cwd": str(env.work), "mcpServers": [_entry(env)]}
        )
        assert "result" in new
        pid = mcp_pid(env)
        assert not gone(pid)
        (relay,) = env.runtime.iterdir()
        assert oct(relay.stat().st_mode & 0o777) == "0o700"
        assert sorted(p.name for p in relay.iterdir()) == ["relay.sock", "tools.json"]
        assert await raw.eof() == 0
        assert gone(pid)
        assert list(env.runtime.iterdir()) == []
        assert env.serve_records() == []
    finally:
        await raw.stop()


@scripted
async def test_a_server_that_exits_mid_session_fails_its_calls(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await run(conn, env, "mcp__test__die", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert done.status == "failed"
        assert "MCP server test exited" in done.content[0].content.text
        editor.forget()
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("mcp__test__echo", text="x"))]
        )
        (later,) = editor.of("tool_call_update")
        assert later.status == "failed"
        assert "MCP server test exited" in later.content[0].content.text


@scripted
async def test_mcp_tools_ask_permission_like_any_tool(env):
    ask = 'permissions.ask=["mcp__test__echo"]'
    async with agent(env, "-e", SCRIPTED, "--set", ask) as (conn, editor, _process):
        await run(conn, env, "mcp__test__echo", mcp_server(env), text="allowed")
        (asked, options) = editor.permissions[0]
        assert "mcp__test__echo" in asked.title
        assert {o.kind for o in options} >= {"allow_once", "reject_once"}
        (done,) = editor.of("tool_call_update")
        assert done.status == "completed"
    async with agent(env, "-e", SCRIPTED, "--set", ask, choice="reject_once") as (
        conn,
        editor,
        _process,
    ):
        await run(conn, env, "mcp__test__echo", mcp_server(env), text="refused")
        assert editor.of("tool_call_update")[-1].status == "failed"
        assert [e["params"]["arguments"]["text"] for e in _calls(env)] == ["allowed"]


def _calls(env):
    return [
        e for e in mcp_log(env) if e.get("method") == "tools/call" and e["params"]["name"] == "echo"
    ]


def _entry(env) -> dict:
    server = mcp_server(env)
    return {
        "name": server.name,
        "command": server.command,
        "args": server.args,
        "env": [{"name": v.name, "value": v.value} for v in server.env],
    }


def test_extension_refuses_to_load_without_the_relay(tmp_path):
    path = Path(__file__).parent.parent / "src" / "tau_acp" / "_extension.py"
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('x', {str(path)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
    )
    for environ, words in (
        ({}, "TAU_ACP_RELAY is not set"),
        ({"TAU_ACP_RELAY": str(tmp_path)}, "cannot read tools.json"),
    ):
        done = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            env={"PATH": "/usr/bin", **environ},
            capture_output=True,
            text=True,
        )
        assert done.returncode != 0
        assert words in done.stderr


# what a server sends that is not what was expected


@scripted
async def test_an_mcp_ping_is_answered_with_an_empty_result(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        await run(conn, env, "mcp__test__ping", mcp_server(env))
        assert editor.of("tool_call_update")[-1].status == "completed"
        answer = next(e for e in mcp_log(env) if e.get("id") == "ping-1")
        assert answer["method"] is None
        assert answer["result"] == {}


@pytest.mark.parametrize("tool", ["malformed", "malformed_block"])
@scripted
async def test_a_malformed_tool_result_fails_the_call_and_keeps_the_server(env, tool):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await run(conn, env, f"mcp__test__{tool}", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert done.status == "failed"
        assert "MCP server test returned a malformed result" in done.content[0].content.text
        editor.forget()  # the server is still in use
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("mcp__test__echo", text="x"))]
        )
        assert editor.of("tool_call_update")[-1].status == "completed"


@pytest.mark.parametrize(("tool", "why"), [("garbage", "not JSON"), ("huge", "longer than")])
@scripted
async def test_a_malformed_line_fails_the_pending_call_not_the_server(env, tool, why):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await run(conn, env, f"mcp__test__{tool}", mcp_server(env))
        (done,) = editor.of("tool_call_update")
        assert done.status == "failed"
        message = done.content[0].content.text
        assert "MCP server test sent a malformed message" in message
        assert why in message
        assert "exited" not in message
        editor.forget()  # it is not gone: the next call goes through
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("mcp__test__echo", text="x"))]
        )
        (later,) = editor.of("tool_call_update")
        assert later.status == "completed"
        assert later.content[0].content.text == "x"


# the tools a relay carries


@scripted
async def test_servers_with_no_tools_load_no_extension(env):
    """τ's tool set is what it is without tau-acp: no -e, no relay environment."""
    async with agent(env) as (conn, _editor, _process):
        server = mcp_server(env, NO_TOOLS="1")
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[server])
        (record,) = env.serve_records()
        assert record["session"] == session.session_id
        pid = record["pid"]
        assert "TAU_ACP_RELAY" not in Path(f"/proc/{pid}/environ").read_text()
        assert b"_extension.py" not in Path(f"/proc/{pid}/cmdline").read_bytes()
        assert [e["method"] for e in mcp_log(env) if e.get("method") == "tools/list"] == []


@scripted
async def test_an_mcp_servers_whole_process_group_is_stopped(env):
    async with agent(env, "-e", SCRIPTED) as (conn, _editor, _process):
        session = await conn.new_session(
            cwd=str(env.work), mcp_servers=[mcp_server(env, SPAWN_CHILD="1")]
        )
        child = next(e["child"] for e in mcp_log(env) if "child" in e)
        assert not gone(child)
        await conn.close_session(session_id=session.session_id)
        await until(lambda: gone(mcp_pid(env)), "the server to stop")
        await until(lambda: gone(child), "its child to stop")
