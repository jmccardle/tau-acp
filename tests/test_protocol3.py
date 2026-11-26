"""What τ's protocol 3 brings, on the wire: user images, the ``Commands`` event, attributed
notices, questions through ``elicitation/create``, ``usage_update``, and that tau-acp has no
``/new`` of its own. Real ``tau serve`` processes; the editor is raw JSON-RPC, because ACP's
elicitation and notice are not in the SDK's stable client."""

import base64
import json
import struct
import zlib
from pathlib import Path

from tests.harness import Raw, scripted

ASKING = str(Path(__file__).with_name("asking.py"))


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + (chunk(b"IEND", b""))
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def image_model(env):
    """The default model takes images (the mock declares the limits D30 asks of a provider)."""
    config = json.loads((env.home / "config.json").read_text())
    config["models"]["m"]["images"] = True
    (env.home / "config.json").write_text(json.dumps(config))


async def start(env, capabilities, *serve_args):
    agent = await Raw.start(env, *serve_args)
    _, response = await agent.request(
        "initialize", {"protocolVersion": 1, "clientCapabilities": capabilities}
    )
    assert "error" not in response, response
    _, response = await agent.request("session/new", {"cwd": str(env.work), "mcpServers": []})
    return agent, response["result"]["sessionId"]


async def prompt(agent, session, *blocks, answer=None):
    """Run a prompt; the updates and requests that came before its answer, and the answer. A
    request from the agent is answered with ``answer(request)`` (its result)."""
    agent.last_id += 1
    mine = agent.last_id
    message = {
        "jsonrpc": "2.0",
        "id": mine,
        "method": "session/prompt",
        "params": {"sessionId": session, "prompt": list(blocks)},
    }
    await agent.write(json.dumps(message).encode())
    seen = []
    while True:
        got = await agent.read()
        if got.get("id") == mine and "method" not in got:
            return seen, got
        seen.append(got)
        if "method" in got and "id" in got:
            result = answer(got) if answer else {}
            reply = {"jsonrpc": "2.0", "id": got["id"], "result": result}
            await agent.write(json.dumps(reply).encode())


def updates(seen, kind=None):
    found = [m["params"]["update"] for m in seen if m.get("method") == "session/update"]
    return [u for u in found if kind is None or u["sessionUpdate"] == kind]


def said(seen):
    return "".join(u["content"]["text"] for u in updates(seen, "agent_message_chunk"))


def asked(seen, method):
    return [m for m in seen if m.get("method") == method]


def image_block(data: bytes) -> dict:
    return {"type": "image", "data": b64(data), "mimeType": "image/png"}


# Images


@scripted
async def test_an_image_prompt_reaches_the_model_and_replays_as_an_image_block(env):
    image_model(env)
    agent, session = await start(env, {})
    try:
        data = png(4, 3)
        text = {"type": "text", "text": "what is this"}
        seen, response = await prompt(agent, session, text, image_block(data))
        assert response["result"] == {"stopReason": "end_turn"}
        assert "[1 image: image/png 4x3]" in said(seen)  # the mock names what it was sent
        assert not updates(seen, "user_message_chunk")  # our own message is not echoed
    finally:
        await agent.stop()
    second, _ = await start(env, {})
    try:
        before, response = await second.request(
            "session/load", {"sessionId": session, "cwd": str(env.work), "mcpServers": []}
        )
        user = [u for u in updates(before) if u["sessionUpdate"] == "user_message_chunk"]
        assert [u["content"]["type"] for u in user] == ["text", "image"]
        assert user[1]["content"] == {
            "type": "image",
            "data": b64(data),
            "mimeType": "image/png",
        }
    finally:
        await second.stop()


@scripted
async def test_an_image_alone_is_a_prompt(env):
    image_model(env)
    agent, session = await start(env, {})
    try:
        seen, response = await prompt(agent, session, image_block(png(2, 2)))
        assert response["result"] == {"stopReason": "end_turn"}
        assert "[1 image: image/png 2x2]" in said(seen)
    finally:
        await agent.stop()


@scripted
async def test_a_model_that_takes_no_images_refuses_the_prompt(env):
    agent, session = await start(env, {})  # the default mock model takes none
    try:
        _, response = await prompt(agent, session, image_block(png(2, 2)))
        assert response["error"]["code"] == -32602
        assert "image" in response["error"]["message"]
    finally:
        await agent.stop()


@scripted
async def test_a_slash_command_takes_no_images(env):
    image_model(env)
    agent, session = await start(env, {})
    try:
        text = {"type": "text", "text": "/usage"}
        _, response = await prompt(agent, session, text, image_block(png(2, 2)))
        assert response["error"]["code"] == -32602
        assert "no images" in response["error"]["message"]
    finally:
        await agent.stop()


# Commands


@scripted
async def test_commands_follow_the_stream_when_they_change(env):
    agent, session = await start(env, {}, "-e", ASKING)
    try:
        first = await agent.read()
        names = {c["name"] for c in first["params"]["update"]["availableCommands"]}
        assert "grow" in names
        assert "grown" not in names
        seen, response = await prompt(agent, session, {"type": "text", "text": "/grow"})
        assert response["result"] == {"stopReason": "end_turn"}
        later = [
            u
            for u in updates(seen, "available_commands_update")
            if "grown" in {c["name"] for c in u["availableCommands"]}
        ]
        assert len(later) == 1  # sent when τ said so, with no call to commands()
    finally:
        await agent.stop()


@scripted
async def test_new_is_not_a_command_tau_acp_has(env):
    agent, session = await start(env, {})
    try:
        first = await agent.read()
        names = {c["name"] for c in first["params"]["update"]["availableCommands"]}
        assert "new" not in names
        seen, response = await prompt(agent, session, {"type": "text", "text": "/new"})
        assert response["result"] == {"stopReason": "end_turn"}
        assert said(seen) == "mock: /new"  # τ has no such command: it is text, as any unknown
    finally:
        await agent.stop()


# Notices


@scripted
async def test_notices_say_who_and_how_loud_as_chat_lines(env):
    agent, session = await start(env, {}, "-e", ASKING)
    try:
        seen, _ = await prompt(agent, session, {"type": "text", "text": "warn"})
        text = said(seen)
        assert "\n[acpasking] warning: careful\n" in text
        assert "\n[acpasking] error: broken\n" in text
        assert not updates(seen, "notice")
    finally:
        await agent.stop()


@scripted
async def test_notices_are_acps_notice_when_the_editor_advertises_it(env):
    agent, session = await start(env, {"session": {"notices": {}}}, "-e", ASKING)
    try:
        seen, _ = await prompt(agent, session, {"type": "text", "text": "warn"})
        found = updates(seen, "notice")
        assert [(u["severity"], u["title"]) for u in found] == [
            ("warning", "[acpasking] careful"),
            ("error", "[acpasking] broken"),
        ]
    finally:
        await agent.stop()


# Questions


FORM = {"elicitation": {"form": {}}}


def accepting(value):
    return lambda request: {"action": "accept", "content": {"answer": value}}


@scripted
async def test_a_question_is_asked_through_elicitation(env):
    agent, session = await start(env, FORM, "-e", ASKING)
    try:
        cases = [("bool", True, "True"), ("str", "hello", "'hello'"), ("pick", "blue", "'blue'")]
        for kind, value, shown in cases:
            seen, response = await prompt(
                agent,
                session,
                {"type": "text", "text": f"ask {kind}"},
                answer=accepting(value),
            )
            assert response["result"] == {"stopReason": "end_turn"}, response
            (request,) = asked(seen, "elicitation/create")
            params = request["params"]
            assert params["sessionId"] == session
            assert params["mode"] == "form"
            assert params["message"] == f"Which ask {kind}?"
            assert list(params["requestedSchema"]["properties"]) == ["answer"]
            assert f"got {shown}" in said(seen)
        enum = params["requestedSchema"]["properties"]["answer"]
        assert enum["enum"] == ["red", "blue"]
    finally:
        await agent.stop()


@scripted
async def test_a_declined_or_cancelled_elicitation_declines_the_question(env):
    agent, session = await start(env, FORM, "-e", ASKING)
    try:
        for action in ("decline", "cancel"):
            seen, response = await prompt(
                agent,
                session,
                {"type": "text", "text": "ask bool"},
                answer=lambda request, action=action: {"action": action},
            )
            assert response["result"] == {"stopReason": "end_turn"}
            assert "got None" in said(seen)
    finally:
        await agent.stop()


@scripted
async def test_a_wrong_answer_is_declined_not_left_open(env):
    agent, session = await start(env, FORM, "-e", ASKING)
    try:
        seen, response = await prompt(
            agent, session, {"type": "text", "text": "ask bool"}, answer=accepting("maybe")
        )
        assert response["result"] == {"stopReason": "end_turn"}
        assert "got None" in said(seen)
    finally:
        await agent.stop()


@scripted
async def test_a_question_a_form_cannot_ask_is_declined_with_a_line(env):
    agent, session = await start(env, FORM, "-e", ASKING)
    try:
        seen, response = await prompt(agent, session, {"type": "text", "text": "ask list"})
        assert response["result"] == {"stopReason": "end_turn"}
        assert not asked(seen, "elicitation/create")
        assert "declined a question ACP cannot carry" in said(seen)
    finally:
        await agent.stop()


@scripted
async def test_an_editor_without_elicitation_has_questions_declined_with_a_line(env):
    agent, session = await start(env, {}, "-e", ASKING)
    try:
        seen, response = await prompt(agent, session, {"type": "text", "text": "ask bool"})
        assert response["result"] == {"stopReason": "end_turn"}
        assert not asked(seen, "elicitation/create")
        assert "declined a question ACP cannot carry: Which ask bool?" in said(seen)
        assert "got None" in said(seen)
    finally:
        await agent.stop()


# Usage


@scripted
async def test_usage_update_follows_a_reply_when_the_model_has_a_context_window(env):
    env.configure()
    config = json.loads((env.home / "config.json").read_text())
    config["models"]["m"]["context_window"] = 100000
    (env.home / "config.json").write_text(json.dumps(config))
    agent, session = await start(env, {})
    try:
        seen, _ = await prompt(agent, session, {"type": "text", "text": "hello world"})
        (usage,) = updates(seen, "usage_update")
        assert usage["size"] == 100000
        assert usage["used"] > 0
        kinds = [u["sessionUpdate"] for u in updates(seen)]
        assert kinds.index("usage_update") > kinds.index("agent_message_chunk")
    finally:
        await agent.stop()


@scripted
async def test_no_usage_update_without_a_context_window(env):
    agent, session = await start(env, {})
    try:
        seen, _ = await prompt(agent, session, {"type": "text", "text": "hello"})
        assert not updates(seen, "usage_update")
    finally:
        await agent.stop()
