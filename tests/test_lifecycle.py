"""A3: list, resume, close, the model config option, a serve that dies, and a session opened
twice."""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime

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

MODEL_OPTION = {
    "id": "model",
    "name": "Model",
    "category": "model",
    "type": "select",
    "options": [{"value": "m", "name": "m"}, {"value": "m2", "name": "m2"}],
}


def option(current: str) -> dict:
    return {**MODEL_OPTION, "currentValue": current}


def prompt(session: str, line: str) -> dict:
    return {"sessionId": session, "prompt": [{"type": "text", "text": line}]}


def slow(seconds: int = 60) -> str:
    return f"call mcp__test__slow {json.dumps({'seconds': seconds})}"


async def start(env, *serve_args: str) -> Raw:
    raw = await Raw.start(env, *serve_args)
    await raw.request("initialize", {"protocolVersion": 1})
    return raw


async def new(raw: Raw, env, servers=()) -> str:
    _, response = await raw.request(
        "session/new", {"cwd": str(env.work), "mcpServers": [s.model_dump() for s in servers]}
    )
    return response["result"]["sessionId"]


def stdio(env, name: str = "test", **variables: str) -> dict:
    server = mcp_server(env, name, **variables).model_dump(by_alias=True)
    return {k: v for k, v in server.items() if v is not None}


def serve_pid(env, session: str) -> int:
    return next(r["pid"] for r in env.serve_records() if r["session"] == session)


# list


@scripted
async def test_list_sessions(env):
    elsewhere = env.home / "elsewhere"
    elsewhere.mkdir()
    async with agent(env) as (conn, _editor, _process):
        first = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=first.session_id, prompt=[text("hello   there\nfriend")])
        named = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=named.session_id, prompt=[text("/name mine")])
        other = await conn.new_session(cwd=str(elsewhere), mcp_servers=[])
        await conn.prompt(session_id=other.session_id, prompt=[text("far away")])
    raw = await start(env)
    try:
        _, response = await raw.request("session/list", {"cwd": str(env.work)})
        sessions = {s["sessionId"]: s for s in response["result"]["sessions"]}
        assert set(sessions) == {first.session_id, named.session_id}
        assert sessions[first.session_id]["title"] == "hello there friend"
        assert sessions[named.session_id]["title"] == "mine"
        for info in sessions.values():
            assert info["cwd"] == str(env.work)
            assert datetime.fromisoformat(info["updatedAt"]).tzinfo is not None
        assert "nextCursor" not in response["result"]

        _, response = await raw.request("session/list", {})
        assert len(response["result"]["sessions"]) == 3  # no cwd: every session

        _, response = await raw.request("session/list", {"cwd": str(env.home / "nowhere")})
        assert response["error"]["code"] == -32602  # not a directory

        _, response = await raw.request("session/list", {"cursor": "x"})
        assert response["error"]["code"] == -32602
        assert "does not page" in response["error"]["message"]
        _, response = await raw.request("session/list", {"cursor": None})
        assert "result" in response
    finally:
        await raw.stop()


@scripted
async def test_list_leaves_out_a_directory_that_is_not_a_session(env):
    (env.home / "sessions" / "junk").mkdir(parents=True)
    raw = await start(env)
    try:
        _, response = await raw.request("session/list", {})
        assert response["result"] == {"sessions": []}
    finally:
        await raw.stop()


# resume


@scripted
async def test_resume_does_not_replay(env):
    first = await start(env)
    session = await new(first, env)
    await first.request("session/prompt", prompt(session, "remember me"))
    assert await first.eof() == 0
    second = await start(env)
    try:
        params = {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        before, response = await second.request("session/resume", params)
        assert before == []  # nothing replayed
        assert response["result"] == {"configOptions": [option("m")]}
        commands = await second.read()
        assert commands["params"]["update"]["sessionUpdate"] == "available_commands_update"
        before, response = await second.request("session/prompt", prompt(session, "and again"))
        assert response["result"] == {"stopReason": "end_turn"}
        streamed = "".join(
            m["params"]["update"]["content"]["text"]
            for m in before
            if m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        )
        assert streamed == "mock: and again"
        _, response = await second.request("session/resume", {**params, "sessionId": "nope"})
        assert response["error"]["code"] == -32002
    finally:
        await second.stop()


# close


@scripted
async def test_close_releases_everything(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        session = await new(raw, env, [mcp_server(env)])
        serve, mcp = serve_pid(env, session), mcp_pid(env)
        assert any(env.runtime.iterdir())  # the relay directory
        _, response = await raw.request("session/close", {"sessionId": session})
        assert response["result"] == {}
        await until(lambda: gone(serve) and gone(mcp), "the serve and the MCP server to stop")
        assert not any(env.runtime.iterdir())
        _, response = await raw.request("session/prompt", prompt(session, "hi"))
        assert response["error"]["code"] == -32002  # it is not held any more
        for ident in (session, "never-existed"):
            _, response = await raw.request("session/close", {"sessionId": ident})
            assert response["result"] == {}
    finally:
        await raw.stop()


@scripted
async def test_close_during_a_prompt_answers_cancelled(env):
    async with agent(env, "-e", SCRIPTED) as (conn, _editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[mcp_server(env)])
        running = asyncio.create_task(
            conn.prompt(session_id=session.session_id, prompt=[text(slow())])
        )
        await until(
            lambda: any(e.get("params", {}).get("name") == "slow" for e in mcp_log(env)),
            "the slow call to reach the MCP server",
        )
        serve, mcp = serve_pid(env, session.session_id), mcp_pid(env)
        await conn.close_session(session_id=session.session_id)
        assert (await running).stop_reason == "cancelled"
        await until(lambda: gone(serve) and gone(mcp), "the serve and the MCP server to stop")


# the model


@scripted
async def test_model_config_option(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        sid = session.session_id
        assert [o.model_dump(by_alias=True, exclude_none=True) for o in session.config_options] == [
            option("m")
        ]
        changed = await conn.set_config_option(session_id=sid, config_id="model", value="m2")
        assert [o.model_dump(by_alias=True, exclude_none=True) for o in changed.config_options] == [
            option("m2")
        ]
        assert editor.of("config_option_update") == []  # the answer carries it
        reply = await conn.prompt(session_id=sid, prompt=[text("on the second")])
        assert reply.stop_reason == "end_turn"
        assert editor.text() == "mock: on the second"
    # τ recorded the change: the session comes back on the second model
    raw = await start(env)
    try:
        params = {"sessionId": sid, "cwd": str(env.work), "mcpServers": []}
        _, response = await raw.request("session/resume", params)
        assert response["result"]["configOptions"] == [option("m2")]
    finally:
        await raw.stop()


@scripted
async def test_bad_config_options_are_refused(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        for params in (
            {"configId": "model", "value": "nope"},
            {"configId": "model", "value": 3},
            {"configId": "model", "type": "boolean", "value": True},
            {"configId": "mode", "value": "m2"},
        ):
            _, response = await raw.request(
                "session/set_config_option", {"sessionId": session, **params}
            )
            assert response["error"]["code"] == -32602, params
        _, response = await raw.request(
            "session/set_config_option", {"sessionId": "nope", "configId": "model", "value": "m"}
        )
        assert response["error"]["code"] == -32002
        _, response = await raw.request("session/prompt", prompt(session, "still works"))
        assert response["result"] == {"stopReason": "end_turn"}
    finally:
        await raw.stop()


@scripted
async def test_model_command_in_a_prompt_updates_the_option(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        before, response = await raw.request("session/prompt", prompt(session, "/model m2"))
        assert response["result"] == {"stopReason": "end_turn"}
        updates = [
            m["params"]["update"]
            for m in before
            if m["params"]["update"]["sessionUpdate"] == "config_option_update"
        ]
        assert updates == [
            {"sessionUpdate": "config_option_update", "configOptions": [option("m2")]}
        ]
    finally:
        await raw.stop()


# a serve that dies


@scripted
async def test_serve_death_mid_prompt(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        session = await new(raw, env, [mcp_server(env)])
        pid = serve_pid(env, session)
        raw.last_id += 1
        waiting = raw.last_id
        await raw.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": waiting,
                    "method": "session/prompt",
                    "params": prompt(session, slow()),
                }
            ).encode()
        )
        await until(
            lambda: any(e.get("params", {}).get("name") == "slow" for e in mcp_log(env)),
            "the slow call to reach the MCP server",
        )
        os.kill(pid, signal.SIGTERM)
        while (got := await raw.read()).get("id") != waiting:
            pass
        assert "error" in got, got
        assert got["error"]["code"] == -32603
        message = f"tau serve for session {session} exited"
        assert message in got["error"]["message"]

        # the session is dead: every request on it says so
        for method, params in (
            ("session/prompt", prompt(session, "hi")),
            (
                "session/set_config_option",
                {"sessionId": session, "configId": "model", "value": "m2"},
            ),
        ):
            _, response = await raw.request(method, params)
            assert response["error"]["code"] == -32603
            assert message in response["error"]["message"]

        # loading it starts it again
        params = {"sessionId": session, "cwd": str(env.work), "mcpServers": [stdio(env)]}
        _, response = await raw.request("session/load", params)
        assert "result" in response
        assert serve_pid(env, session) != pid
        _, response = await raw.request("session/prompt", prompt(session, "back again"))
        assert response["result"] == {"stopReason": "end_turn"}
    finally:
        await raw.stop()


@scripted
async def test_serve_death_while_idle_then_resume(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        await raw.request("session/prompt", prompt(session, "first"))
        pid = serve_pid(env, session)
        os.kill(pid, signal.SIGTERM)
        await until(lambda: gone(pid), "the serve to exit")
        _, response = await raw.request("session/prompt", prompt(session, "hi"))
        assert response["error"]["code"] == -32603
        assert "exited" in response["error"]["message"]
        params = {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        _, response = await raw.request("session/resume", params)
        assert "result" in response
        _, response = await raw.request("session/prompt", prompt(session, "again"))
        assert response["result"] == {"stopReason": "end_turn"}
    finally:
        await raw.stop()


# a session opened twice


@scripted
async def test_load_of_a_held_session_checks_its_mcp_servers(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        session = await new(raw, env, [mcp_server(env)])
        params = {"sessionId": session, "cwd": str(env.work)}
        for method in ("session/load", "session/resume"):
            for servers in ([], [stdio(env, PAGE_SIZE="3")], [stdio(env, "other")]):
                _, response = await raw.request(method, {**params, "mcpServers": servers})
                assert response["error"]["code"] == -32602
                assert (
                    f"session {session} is already open with different MCP servers"
                    in (response["error"]["message"])
                )
        # the same list is fine, and does not restart anything
        pid = mcp_pid(env)
        _, response = await raw.request("session/resume", {**params, "mcpServers": [stdio(env)]})
        assert "result" in response
        assert mcp_pid(env) == pid
        assert len([e for e in mcp_log(env) if "started" in e]) == 1
        await raw.request("session/close", {"sessionId": session})
    finally:
        await raw.stop()


# releasing a session: only what is ours, whatever the state of the serve


async def other_serve(env, session: str) -> int:
    """Start a serve for ``session`` outside tau-acp (as another editor or a user might)."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *("-m", "tau", "serve", "-d", "--json", "--session", session),
        cwd=env.work,
        env={**os.environ, **env.environ},
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    return json.loads(out.splitlines()[-1])["pid"]


@scripted
async def test_closing_a_dead_session_does_not_stop_the_serve_that_replaced_it(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        await raw.request("session/prompt", prompt(session, "hi"))
        pid = serve_pid(env, session)
        os.kill(pid, signal.SIGTERM)
        await until(lambda: gone(pid), "the serve to exit")
        _, response = await raw.request("session/prompt", prompt(session, "x"))
        assert "exited" in response["error"]["message"]  # tau-acp has found it dead
        other = await other_serve(env, session)  # someone else serves the session now
        assert other != pid
        _, response = await raw.request("session/close", {"sessionId": session})
        assert response["result"] == {}
        await asyncio.sleep(1.5)  # a wrong stop is not instant
        assert not gone(other)
        assert await raw.eof() == 0
        assert not gone(other)  # nor is it stopped when tau-acp exits
    finally:
        await raw.stop()
        env.kill_serves()


# a session is opened by its full id only


@scripted
async def test_load_by_a_prefix_or_name_is_refused_and_nothing_is_left_running(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        await raw.request("session/prompt", prompt(session, "hi"))
        await raw.request("session/close", {"sessionId": session})
        assert env.serve_records() == []
        for method in ("session/load", "session/resume"):
            params = {"sessionId": session[:-3], "cwd": str(env.work), "mcpServers": []}
            _, response = await raw.request(method, params)
            assert response["error"]["code"] == -32002, response
            assert response["error"]["data"] == {"sessionId": session[:-3]}
            assert session in response["error"]["message"]
            assert env.serve_records() == []  # the serve it started for the check is stopped
            _, response = await raw.request("session/prompt", prompt(session[:-3], "again"))
            assert response["error"]["code"] == -32002
        _, response = await raw.request(
            "session/load", {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        )
        assert "result" in response
    finally:
        await raw.stop()


@scripted
async def test_load_by_a_prefix_of_a_held_session_does_not_stop_it(env):
    raw = await start(env)
    try:
        session = await new(raw, env)
        params = {"sessionId": session[:-3], "cwd": str(env.work), "mcpServers": []}
        _, response = await raw.request("session/resume", params)
        assert response["error"]["code"] == -32002
        _, response = await raw.request("session/prompt", prompt(session, "still mine"))
        assert response["result"] == {"stopReason": "end_turn"}
    finally:
        await raw.stop()


# going away in the middle of starting


@scripted
async def test_eof_during_session_new_leaves_no_serve(env):
    raw = await start(env)
    try:
        message = {"cwd": str(env.work), "mcpServers": []}
        await raw.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": message}
            ).encode()
        )
        await asyncio.sleep(0.15)  # `tau serve -d` takes about a second
        assert await raw.eof() == 0
        await asyncio.sleep(5)  # long enough for the serve to have been started, and stopped
        assert env.serve_records() == []
    finally:
        await raw.stop()


@scripted
async def test_eof_during_session_new_releases_the_mcp_servers(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        servers = [mcp_server(env).model_dump(by_alias=True, exclude_none=True)]
        message = {"cwd": str(env.work), "mcpServers": servers}
        await raw.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": message}
            ).encode()
        )
        await until(lambda: mcp_log(env), "the MCP server to start")
        assert await raw.eof() == 0
        await until(lambda: gone(mcp_pid(env)), "the MCP server to stop")
        await asyncio.sleep(5)
        assert env.serve_records() == []
        assert list(env.runtime.iterdir()) == []
    finally:
        await raw.stop()


# closing when the serve dies under the close


@scripted
async def test_close_releases_everything_when_the_serve_dies_during_it(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        session = await new(raw, env, [mcp_server(env)])
        pid = serve_pid(env, session)
        mcp = mcp_pid(env)
        raw.last_id += 1
        await raw.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": raw.last_id,
                    "method": "session/prompt",
                    "params": prompt(session, slow()),
                }
            ).encode()
        )
        await until(
            lambda: any(e.get("params", {}).get("name") == "slow" for e in mcp_log(env)),
            "the slow call to reach the MCP server",
        )
        os.kill(pid, signal.SIGSTOP)  # the abort that close sends gets no answer ...
        closing = asyncio.ensure_future(raw.request("session/close", {"sessionId": session}))
        await asyncio.sleep(0.5)
        os.kill(pid, signal.SIGKILL)  # ... until the connection is lost
        _, response = await closing
        assert response["result"] == {}
        await until(lambda: gone(mcp), "the MCP server to stop")
        assert list(env.runtime.iterdir()) == []
    finally:
        await raw.stop()


@scripted
async def test_eof_releases_every_session(env):
    raw = await start(env, "-e", SCRIPTED)
    try:
        for name in ("one", "two"):
            await new(raw, env, [mcp_server(env, name)])
        assert len(env.serve_records()) == 2
        assert await raw.eof() == 0
        assert env.serve_records() == []
        assert list(env.runtime.iterdir()) == []
        for name in ("one", "two"):
            assert gone(mcp_pid(env, name))
    finally:
        await raw.stop()


@scripted
async def test_a_load_that_fails_after_the_serve_started_holds_and_leaves_nothing(env):
    elsewhere = env.home / "elsewhere"
    elsewhere.mkdir()
    raw = await start(env)
    try:
        session = await new(raw, env)
        await raw.request("session/prompt", prompt(session, "hi"))
        await raw.request("session/close", {"sessionId": session})
        params = {"sessionId": session, "cwd": str(elsewhere), "mcpServers": [stdio(env)]}
        _, response = await raw.request("session/load", params)
        assert response["error"]["code"] == -32602
        assert "was started in" in response["error"]["message"]
        assert env.serve_records() == []  # the serve the load started is stopped
        _, response = await raw.request("session/prompt", prompt(session, "x"))
        assert response["error"]["code"] == -32002  # and the session is not held
        assert list(env.runtime.iterdir()) == []  # nor its relay directory
    finally:
        await raw.stop()
