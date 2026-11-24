"""The events of a session are always being read: between prompts, while another cursor writes,
and when the serve goes away under a prompt."""

import asyncio
import json
import os
import signal
from pathlib import Path

from tau import RemoteClient

from tests.harness import SCRIPTED, Raw, agent, gone, scripted, text, until

STUBBORN = str(Path(__file__).with_name("stubborn.py"))


def prompt(session: str, line: str) -> dict:
    return {"sessionId": session, "prompt": [{"type": "text", "text": line}]}


async def other_client(env, session: str) -> RemoteClient:
    """Another client of the session's serve: whatever else is attached to a τ session."""
    (record,) = [r for r in env.serve_records() if r["session"] == session]
    return await RemoteClient.connect(record["socket"], "other")


# between prompts


@scripted
async def test_a_model_changed_from_outside_is_announced_between_prompts(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=session.session_id, prompt=[text("hello")])
        editor.forget()
        other = await other_client(env, session.session_id)
        try:
            await other.run(0, "model", {"model": "m2"})
            await until(lambda: editor.of("config_option_update"), "the announcement", 10)
        finally:
            await other.close()
        (update,) = editor.of("config_option_update")
        (option,) = update.config_options
        assert option.current_value == "m2"


@scripted
async def test_what_happens_on_the_line_between_prompts_is_sent_at_once(env):
    """Another client's turn on this cursor is heard when it happens, so the next prompt does not
    start on its stale events (the end of that turn is not the end of this prompt)."""
    env.configure(delay_ms=150)
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        other = await other_client(env, session.session_id)
        try:
            await other.send(0, "from the other client")
            await until(
                lambda: "from the other client" in editor.text("user_message_chunk"), "it", 15
            )
            await until(lambda: "mock: from the other client" in editor.text(), "its reply", 15)
        finally:
            await other.close()
        editor.forget()
        reply = await conn.prompt(
            session_id=session.session_id, prompt=[text("one two three four five")]
        )
        assert reply.stop_reason == "end_turn"
        assert editor.text().split() == ["mock:", "one", "two", "three", "four", "five"]


@scripted
async def test_nodes_of_another_cursor_are_not_this_prompts_output(env):
    env.configure(delay_ms=150)
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        turn = asyncio.ensure_future(
            conn.prompt(
                session_id=session.session_id,
                prompt=[text("alpha beta gamma delta epsilon zeta eta theta")],
            )
        )
        await until(lambda: editor.of("agent_message_chunk"), "the first words", 20)
        other = await other_client(env, session.session_id)
        try:
            cursor = await other.open(1)  # a branch from the system prompt
            await other.send(cursor, "intruder words")
            await asyncio.sleep(1.0)  # its reply is appended while ours is still streaming
            reply = await turn
        finally:
            await other.close()
        assert reply.stop_reason == "end_turn"
        assert "intruder" not in editor.text()
        assert "intruder" not in editor.text("user_message_chunk")
        assert editor.text().split()[-1] == "theta"


# a progress update carries the tail of the output


@scripted
async def test_progress_updates_carry_the_tail_of_the_output_not_all_of_it(env):
    script = (
        "for i in 1 2 3 4; do head -c 40000 /dev/zero | tr '\\\\0' x; sleep 0.5; done; echo END"
    )
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        line = "call bash " + json.dumps({"command": script})
        await conn.prompt(session_id=session.session_id, prompt=[text(line)])
        running = [u for u in editor.of("tool_call_update") if u.status == "in_progress"]
        assert len(running) >= 2
        sizes = [len(u.content[0].content.text) for u in running]
        assert max(sizes) <= 65536, sizes
        assert sizes[-1] == 65536  # the last of 160000 characters, cut to the tail


# the serve goes away


async def new(raw: Raw, env) -> str:
    _, response = await raw.request("session/new", {"cwd": str(env.work), "mcpServers": []})
    return response["result"]["sessionId"]


async def send_prompt(raw: Raw, session: str, line: str) -> int:
    raw.last_id += 1
    await raw.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": raw.last_id,
                "method": "session/prompt",
                "params": prompt(session, line),
            }
        ).encode()
    )
    return raw.last_id


async def response_to(raw: Raw, ident: int) -> dict:
    while (got := await raw.read()).get("id") != ident or "method" in got:
        pass
    return got


@scripted
async def test_a_serve_that_outlives_its_stream_is_still_found_gone(env):
    """τ aborts the turn, ends the stream, and its process stays up (a thread that cannot be
    interrupted): the stream, not the pid, says the serve exited."""
    raw = await Raw.start(env, "-e", SCRIPTED, "-e", STUBBORN)
    try:
        await raw.request("initialize", {"protocolVersion": 1})
        session = await new(raw, env)
        ident = await send_prompt(raw, session, "call stubborn {}")
        while (await raw.read()).get("params", {}).get("update", {}).get("sessionUpdate") != (
            "tool_call"
        ):
            pass
        (record,) = env.serve_records()
        os.kill(record["pid"], signal.SIGTERM)
        got = await response_to(raw, ident)
        assert got["error"]["code"] == -32603
        assert f"tau serve for session {session} exited" in got["error"]["message"]
        assert not gone(record["pid"])  # it was found gone without the pid saying so
    finally:
        await raw.stop()
        env.kill_serves()


# cancel


@scripted
async def test_a_prompt_cancelled_answers_cancelled_even_when_the_serve_dies(env):
    env.configure(delay_ms=400, reply="one two three four five six seven eight nine ten")
    raw = await Raw.start(env)
    try:
        await raw.request("initialize", {"protocolVersion": 1})
        session = await new(raw, env)
        ident = await send_prompt(raw, session, "go")
        while (await raw.read()).get("method") != "session/update":
            pass
        (record,) = env.serve_records()
        os.kill(record["pid"], signal.SIGSTOP)  # the abort gets no answer ...
        await raw.notify("session/cancel", {"sessionId": session})
        await asyncio.sleep(0.5)
        os.kill(record["pid"], signal.SIGKILL)  # ... the serve dies
        got = await response_to(raw, ident)
        assert got["result"] == {"stopReason": "cancelled"}
        _, response = await raw.request("session/prompt", prompt(session, "again"))
        assert "exited" in response["error"]["message"]  # but the session is known to be dead
    finally:
        await raw.stop()
        env.kill_serves()


@scripted
async def test_a_cancel_while_the_line_is_being_read_runs_no_command(env):
    raw = await Raw.start(env)
    try:
        await raw.request("initialize", {"protocolVersion": 1})
        session = await new(raw, env)
        (record,) = env.serve_records()
        os.kill(record["pid"], signal.SIGSTOP)  # parse_line waits for the serve
        ident = await send_prompt(raw, session, "/usage")
        await asyncio.sleep(0.5)
        await raw.notify("session/cancel", {"sessionId": session})
        await asyncio.sleep(0.5)
        os.kill(record["pid"], signal.SIGCONT)
        before: list[dict] = []
        while (got := await raw.read()).get("id") != ident:
            before.append(got)
        assert got["result"] == {"stopReason": "cancelled"}
        updates = [m["params"]["update"]["sessionUpdate"] for m in before if "params" in m]
        assert "agent_message_chunk" not in updates  # /usage did not run
    finally:
        await raw.stop()
        env.kill_serves()
