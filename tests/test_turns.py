"""How a turn ends when τ stops it (the step limit, a hook), and what τ's protocol 2 adds to a turn:
images in tool results. Real ``tau serve`` processes on the ``mock`` provider."""

import json
from pathlib import Path

import pytest
from acp import RequestError

from tests.harness import agent, scripted, text
from tests.test_translate import png

LOOPING = str(Path(__file__).with_name("looping.py"))
PICTURE = str(Path(__file__).with_name("picture.py"))


@scripted
async def test_the_step_limit_ends_the_turn_with_max_turn_requests(env):
    env.configure(extensions={"step_limit": {"limit": 3}})
    (env.work / "loop.txt").write_text("again\n")
    async with agent(env, "-e", LOOPING) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("loop")])
        assert reply.stop_reason == "max_turn_requests"
        assert len(editor.of("tool_call")) == 3  # one call a step, and three steps
        # the session goes on: the next prompt starts a new count
        editor.forget()
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("loop")])
        assert reply.stop_reason == "max_turn_requests"
        assert len(editor.of("tool_call")) == 3


@scripted
async def test_a_hook_that_turns_auto_step_off_is_not_the_step_limit(env):
    (env.work / "loop.txt").write_text("again\n")
    async with agent(env, "-e", LOOPING) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        with pytest.raises(RequestError) as raised:
            await conn.prompt(session_id=session.session_id, prompt=[text("pause")])
        assert raised.value.code == -32603
        assert "auto-step is off" in str(raised.value)
        assert len(editor.of("tool_call")) == 1


def _shown(editor) -> list[str]:
    return [
        block.content.text
        for update in editor.of("tool_call_update")
        if update.status == "completed"
        for block in update.content or ()
    ]


def _seeing_model(env) -> None:
    """Make the default model one that takes images, on a provider that declares image limits
    (``openai``); ``picture.py`` drafts every reply, so its server is never contacted."""
    config = json.loads((env.home / "config.json").read_text())
    config["models"]["m"] = {
        "model": "m",
        "provider": "openai",
        "base_url": "http://127.0.0.1:9/v1",
        "images": True,
    }
    (env.home / "config.json").write_text(json.dumps(config))


@scripted
async def test_an_image_in_a_tool_result_is_shown_as_its_line_live_and_on_load(env):
    _seeing_model(env)
    (env.work / "pic.png").write_bytes(png(4, 3))
    async with agent(env, "-e", PICTURE) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("show pic.png")])
        assert reply.stop_reason == "end_turn"
        (live,) = _shown(editor)
        assert live.endswith("\n[image 4x3 image/png]")
        await conn.close_session(session_id=session.session_id)
        editor.forget()
        # loaded from disk, the node's image has no bytes: the line is the same
        await conn.load_session(cwd=str(env.work), session_id=session.session_id, mcp_servers=[])
        assert _shown(editor) == [live]
