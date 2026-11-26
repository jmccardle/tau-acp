import pytest
from acp import RequestError
from acp.schema import (
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    ResourceContentBlock,
    TextResourceContents,
)

from tests.harness import agent, scripted, text, until


@scripted
async def test_initialize_shape(env):
    async with agent(env) as (conn, _editor, _process):
        init = await conn.initialize(
            protocol_version=1, client_info=Implementation(name="t", version="0")
        )
        dump = init.model_dump(by_alias=True, exclude_none=True)
        assert dump["protocolVersion"] == 1
        assert dump["authMethods"] == []
        assert dump["agentInfo"]["name"] == "tau-acp"
        caps = dump["agentCapabilities"]
        assert caps["loadSession"] is True
        assert caps["promptCapabilities"] == {
            "image": True,
            "audio": False,
            "embeddedContext": True,
        }
        assert caps["mcpCapabilities"]["http"] is False
        assert caps["mcpCapabilities"]["sse"] is False


@scripted
async def test_new_prompt_end_turn(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        assert session.session_id == env.serve_records()[0]["session"]
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("hello world")])
        assert reply.stop_reason == "end_turn"
        assert editor.text() == "mock: hello world"  # streamed once, not again from the node
        await until(lambda: editor.of("available_commands_update"), "the command list")
        names = {c.name for c in editor.of("available_commands_update")[0].available_commands}
        assert {"model", "usage"} <= names


@scripted
async def test_two_prompts_in_one_session(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=session.session_id, prompt=[text("one")])
        editor.forget()
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("two")])
        assert reply.stop_reason == "end_turn"
        assert editor.text() == "mock: two"


@scripted
async def test_prompt_content_blocks(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        blocks = [
            text("look"),
            ResourceContentBlock(type="resource_link", name="a.py", uri="file:///a.py"),
            EmbeddedResourceContentBlock(
                type="resource",
                resource=TextResourceContents(uri="file:///b.py", text="x = 1"),
            ),
        ]
        reply = await conn.prompt(session_id=session.session_id, prompt=blocks)
        assert reply.stop_reason == "end_turn"
        assert editor.text() == (
            "mock: look\n\n[a.py](file:///a.py)\n\nfile:///b.py\n```\nx = 1\n```"
        )
        image = ImageContentBlock(type="image", data="AAAA", mime_type="image/png")
        with pytest.raises(RequestError) as caught:
            await conn.prompt(session_id=session.session_id, prompt=[image])
        assert caught.value.code == -32602


@scripted
async def test_slash_command_runs(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("/usage")])
        assert reply.stop_reason == "end_turn"
        assert editor.text()  # the command's result, as a message


@scripted
async def test_unknown_slash_command_is_text(env):
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("/foo bar")])
        assert reply.stop_reason == "end_turn"
        assert editor.text() == "mock: /foo bar"


@scripted
async def test_command_missing_arguments(env):
    async with agent(env) as (conn, _editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        with pytest.raises(RequestError) as caught:
            await conn.prompt(session_id=session.session_id, prompt=[text("/model")])
        assert caught.value.code == -32602
        assert "model" in caught.value.args[0] or "needs" in str(caught.value.data)


@scripted
async def test_unknown_session_prompt(env):
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.prompt(session_id="nope", prompt=[text("hi")])
        assert caught.value.code == -32002


@scripted
async def test_new_session_without_a_model_fails_early(env):
    (env.home / "config.json").write_text("{}")
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.new_session(cwd=str(env.work), mcp_servers=[])
        assert caught.value.code == -32603
        assert "no model chosen" in str(caught.value.data) or "no model chosen" in str(caught.value)


@scripted
async def test_load_replays_a_previous_turn(env):
    async with agent(env) as (conn, _editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=session.session_id, prompt=[text("remember me")])
    pid = None  # the first agent is gone: its serve stopped with it
    assert env.serve_records() == [], pid

    async with agent(env) as (conn, editor, _process):
        await conn.load_session(cwd=str(env.work), session_id=session.session_id, mcp_servers=[])
        await until(lambda: editor.of("agent_message_chunk"), "the replayed reply")
        assert editor.text("user_message_chunk") == "remember me"
        assert editor.text() == "mock: remember me"
        # the loaded session takes a new turn
        editor.forget()
        reply = await conn.prompt(session_id=session.session_id, prompt=[text("and again")])
        assert reply.stop_reason == "end_turn"
        assert editor.text() == "mock: and again"


@scripted
async def test_load_unknown_session(env):
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.load_session(cwd=str(env.work), session_id="nope", mcp_servers=[])
        assert caught.value.code == -32002


@scripted
async def test_load_from_another_directory_is_refused(env):
    async with agent(env) as (conn, _editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(session_id=session.session_id, prompt=[text("hi")])
    elsewhere = env.home / "elsewhere"
    elsewhere.mkdir()
    async with agent(env) as (conn, _editor, _process):
        with pytest.raises(RequestError) as caught:
            await conn.load_session(
                cwd=str(elsewhere), session_id=session.session_id, mcp_servers=[]
            )
        assert caught.value.code == -32602
        assert env.serve_records() == []  # the serve it started for the check was stopped
