"""The pure translation functions, on τ's public value types."""

import base64
import json
import struct
import zlib

import pytest
from tau import (
    Author,
    Body,
    CursorView,
    Ending,
    Form,
    Image,
    Notice,
    OpenQuestion,
    Outcome,
    Part,
    Stop,
)

from tau_acp import translate


def block(**fields):
    return fields


def test_prompt_text_joins_blocks_with_a_blank_line():
    blocks = [
        block(type="text", text="look at this"),
        block(type="resource_link", name="a.py", uri="file:///a.py"),
        block(type="resource", resource={"uri": "file:///b.py", "text": "x = 1"}),
    ]
    assert translate.prompt_content(blocks) == (
        "look at this\n\n[a.py](file:///a.py)\n\nfile:///b.py\n```\nx = 1\n```",
        [],
    )


def test_embedded_text_gets_a_fence_longer_than_its_backticks():
    resource = {"uri": "u", "text": "```py\nx\n```"}
    assert translate.prompt_content([block(type="resource", resource=resource)])[0] == (
        "u\n````\n```py\nx\n```\n````"
    )


@pytest.mark.parametrize(
    "bad",
    [
        block(type="audio", data="AA", mimeType="audio/wav"),
        block(type="resource", resource={"uri": "u", "blob": "AA"}),
        block(type="mystery"),
    ],
)
def test_prompt_text_refuses_what_tau_cannot_take(bad):
    with pytest.raises(ValueError, match=r"cannot be sent|binary"):
        translate.prompt_content([bad])


def test_prompt_text_refuses_an_empty_prompt():
    with pytest.raises(ValueError, match="empty"):
        translate.prompt_content([])


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("read", "read"),
        ("write", "edit"),
        ("edit", "edit"),
        ("grep", "search"),
        ("find", "search"),
        ("bash", "execute"),
        ("web_search", "other"),
    ],
)
def test_tool_kind(tool, kind):
    assert translate.tool_kind(tool) == kind


def test_titles_name_the_path_the_command_or_the_arguments():
    assert translate.tool_title("read", '{"path": "src/a.py"}') == "read: src/a.py"
    assert translate.tool_title("bash", '{"command": "ls -l\\nrm x"}') == "bash: ls -l"
    assert translate.tool_title("other", '{"q": 1}') == 'other: {"q": 1}'
    assert translate.tool_title("other", "not json") == "other: not json"
    assert translate.tool_title("other", "") == "other"
    long = translate.tool_title("bash", '{"command": "' + "x" * 200 + '"}')
    assert len(long) == len("bash: ") + translate.TITLE_WIDTH
    assert long.endswith("…")


def test_locations_resolve_against_the_cwd():
    assert translate.tool_locations("read", '{"path": "a/../b.py"}', "/w") == [{"path": "/w/b.py"}]
    assert translate.tool_locations("write", '{"path": "/abs/c"}', "/w") == [{"path": "/abs/c"}]
    assert translate.tool_locations("bash", '{"command": "ls"}', "/w") == []
    assert translate.tool_locations("read", "not json", "/w") == []


def test_tool_call_ids_carry_the_node():
    call = Part.Call("c1", "read", '{"path": "a"}')
    update = translate.tool_call(7, call, "/w")
    assert update["toolCallId"] == "7:c1"
    assert update["kind"] == "read"
    assert update["status"] == "pending"
    assert update["rawInput"] == {"path": "a"}
    assert update["locations"] == [{"path": "/w/a"}]
    raw = translate.tool_call(7, Part.Call("c2", "x", "oops"), "/w")
    assert raw["rawInput"] == "oops"
    assert "locations" not in raw


def test_outcomes_become_statuses():
    ok = translate.outcome_update(3, "c1", Outcome.Ok("fine"))
    assert (ok["toolCallId"], ok["status"], ok["rawOutput"]) == ("3:c1", "completed", "fine")
    assert ok["content"][0]["content"] == {"type": "text", "text": "fine"}
    assert translate.outcome_update(3, "c1", Outcome.Err("bad"))["status"] == "failed"
    interrupted = translate.outcome_update(3, "c1", Outcome.Interrupted())
    assert interrupted["status"] == "failed"
    assert interrupted["rawOutput"] == "interrupted"


class Node:
    """The fields of a τ node the translation reads (τ makes its own)."""

    def __init__(self, id, body, parent=None):
        self.id, self.body, self.parent, self.by = id, body, parent, Author.Provider()


def assistant(parts, id=4):
    return Node(id, Body.Assistant(parts, Stop.End), parent=id - 1)


def texts(updates):
    return [(u["sessionUpdate"], u["content"]["text"]) for u in updates]


def test_assistant_node_sends_text_in_order_with_its_calls():
    node = assistant([Part.Thinking("hm"), Part.Text("go"), Part.Call("c", "bash", "{}")])
    updates = translate.assistant_updates(node, "/w")
    assert [u["sessionUpdate"] for u in updates] == [
        "agent_thought_chunk",
        "agent_message_chunk",
        "tool_call",
    ]


def test_assistant_text_streamed_already_is_not_sent_again():
    node = assistant([Part.Text("hello world")])
    assert translate.assistant_updates(node, "/w", text_seen="hello world") == []
    assert texts(translate.assistant_updates(node, "/w", text_seen="hello ")) == [
        ("agent_message_chunk", "world")
    ]


def test_assistant_text_that_differs_from_the_stream_says_so():
    node = assistant([Part.Text("hello world")])
    (update,) = translate.assistant_updates(node, "/w", text_seen="help")
    assert update["content"]["text"] == f"{translate.STREAM_BREAK}\nhello world"


def test_replay_of_a_walk():
    nodes = [
        Node(0, Body.System("be brief")),
        Node(1, Body.User("hi"), 0),
        assistant([Part.Text("calling"), Part.Call("c", "bash", "{}")], id=2),
        Node(3, Body.Results([("c", Outcome.Ok("done"), Author.Tools())]), 2),
        assistant([Part.Text("ok")], id=4),
    ]
    updates = translate.replay_updates(nodes, "/w", {})
    assert [u["sessionUpdate"] for u in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[3]["toolCallId"] == updates[2]["toolCallId"] == "2:c"


def test_command_names_qualify_only_when_two_share_a_name():
    def form(name, extension):
        return Form(name, f"does {name}", {"type": "object"}, extension)

    forms = [form("model", "tau"), form("save", "a"), form("save", "b")]
    assert list(translate.command_names(forms)) == ["model", "a:save", "b:save"]


def test_available_commands_hint_names_the_arguments():
    schema = {
        "type": "object",
        "properties": {"focus": {"type": "string"}, "note": {"type": "string"}},
        "required": ["focus"],
    }
    forms = [Form("compact", "Summarize", schema, "compaction")]
    update = translate.available_commands(forms)
    assert update["sessionUpdate"] == "available_commands_update"
    assert update["availableCommands"] == [
        {"name": "compact", "description": "Summarize", "input": {"hint": "<focus> [note]"}}
    ]


def test_permission_answers():
    def chosen(option):
        return {"outcome": {"outcome": "selected", "optionId": option}}

    assert translate.permission_answer(chosen("yes")) == "yes"
    assert translate.permission_answer(chosen("always")) == "always"
    assert translate.permission_answer(chosen("no")) == "no"
    assert translate.permission_answer(chosen("allow-sure")) is None
    assert translate.permission_answer({"outcome": {"outcome": "cancelled"}}) is None
    assert translate.permission_answer(None) is None
    assert translate.permission_answer("yes") is None


def view(ending, *, busy=False, auto=True, items=()):
    return CursorView(0, 5, busy, auto, (), items, ending, None, "m", ())


@pytest.mark.parametrize(
    ("stop", "reason"),
    [(Stop.End, "end_turn"), (Stop.Length, "max_tokens"), (Stop.Refused, "refusal")],
)
def test_done_takes_the_last_reply_stop(stop, reason):
    assert translate.stop_reason(view(Ending.Done()), stop, message=True, cancelled=False) == reason


def test_aborted_and_cancelled_are_cancelled():
    aborted = view(Ending.Aborted())
    assert translate.stop_reason(aborted, None, message=True, cancelled=False) == "cancelled"
    failed = view(Ending.Failed("boom"))
    assert translate.stop_reason(failed, None, message=True, cancelled=True) == "cancelled"


@pytest.mark.parametrize(
    ("ending", "words"),
    [
        (Ending.Failed("boom"), "boom"),
        (Ending.Halted("too many steps"), "halted: too many steps"),
        (None, "no recorded ending"),
    ],
)
def test_other_endings_are_errors(ending, words):
    with pytest.raises(translate.TurnError, match=words):
        translate.stop_reason(view(ending), Stop.End, message=True, cancelled=False)


def test_a_message_must_have_been_taken_up():
    with pytest.raises(translate.TurnError, match="auto-step is off"):
        translate.stop_reason(
            view(Ending.Done(), auto=False), Stop.End, message=True, cancelled=False
        )
    done = view(Ending.Done(), auto=False)
    assert translate.stop_reason(done, None, message=False, cancelled=False) == "end_turn"


def test_the_step_limit_is_max_turn_requests():
    rested = view(Ending.Done(), auto=False)
    for message in (True, False):
        reason = translate.stop_reason(
            rested, Stop.Tools, message=message, cancelled=False, limited=True
        )
        assert reason == "max_turn_requests"
    assert (  # a cancel wins
        translate.stop_reason(rested, Stop.Tools, message=True, cancelled=True, limited=True)
        == "cancelled"
    )
    with pytest.raises(translate.TurnError, match="still pending"):
        translate.stop_reason(
            view(Ending.Done(), auto=False, items=(1,)),
            Stop.Tools,
            message=True,
            cancelled=False,
            limited=True,
        )


def test_an_unusable_last_stop_is_an_error():
    with pytest.raises(translate.TurnError, match="no reply"):
        translate.stop_reason(view(Ending.Done()), None, message=True, cancelled=False)
    with pytest.raises(translate.TurnError, match="ended with"):
        translate.stop_reason(view(Ending.Done()), Stop.Error, message=True, cancelled=False)


def question(text, id=0):
    return OpenQuestion(id, None, "permissions", text, {"enum": ["yes", "always", "no"]})


def asked(question_text, *calls, taken=None, cwd="/w"):
    node = Node(4, Body.Assistant(list(calls), Stop.Tools), parent=3)
    return translate.permission_tool_call(
        question(question_text), node, set() if taken is None else taken, cwd
    )


def test_a_permission_question_is_matched_to_the_call_with_the_same_shown_text():
    first = Part.Call("c1", "write", '{"path": "safe.txt"}')
    second = Part.Call("c2", "write", '{"path": "secret.txt"}')
    update = asked("write: /w/secret.txt\n(ask rule: write)", first, second)
    assert update["toolCallId"] == "4:c2"
    assert update["rawInput"] == {"path": "secret.txt"}
    assert update["content"][0]["content"]["text"] == "write: /w/secret.txt\n(ask rule: write)"


@pytest.mark.parametrize(
    ("call", "shown"),
    [
        (Part.Call("c", "bash", '{"command": "ls -l"}'), "bash: ls -l"),
        (Part.Call("c", "bash", '{"command": 3}'), 'bash: {"command": 3}'),
        (Part.Call("c", "web", '{"q": "x"}'), 'web: {"q": "x"}'),
        (Part.Call("c", "read", '{"path": "../a"}'), "read: /a"),
        (Part.Call("c", "grep", '{"pattern": "x"}'), "grep: /w"),
        (
            Part.Call("c", "grep", '{"path": "a", "files": ["b", "c"]}'),
            "grep: /w/a\ngrep: /w/b\ngrep: /w/c",
        ),
        (Part.Call("c", "read", '{"path": 4}'), 'read: {"path": 4}'),
    ],
)
def test_shown_is_what_tau_builds_for_its_question(call, shown):
    assert translate.shown(call.tool, call.args_raw, "/w") == shown
    assert asked(f"{shown}\n(ask rule: x)", call)["toolCallId"] == "4:c"


def test_identical_calls_are_taken_in_turn():
    calls = [
        Part.Call("c1", "bash", '{"command": "x"}'),
        Part.Call("c2", "bash", '{"command": "x"}'),
    ]
    taken: set[str] = set()
    ids = [asked("bash: x\n(ask rule: bash)", *calls, taken=taken)["toolCallId"] for _ in range(3)]
    assert ids == ["4:c1", "4:c2", "question:0"]


def test_a_question_that_matches_no_call_is_shown_whole():
    update = asked("bash: rm -rf /\n(ask rule: bash)", Part.Call("c1", "bash", '{"command": "ls"}'))
    assert update == {
        "toolCallId": "question:0",
        "title": "bash: rm -rf /\n(ask rule: bash)",
        "content": [
            {
                "type": "content",
                "content": {"type": "text", "text": "bash: rm -rf /\n(ask rule: bash)"},
            }
        ],
    }


def test_an_ambiguous_question_is_shown_whole():
    """A command that contains τ's own marker makes two calls' texts prefixes of one another."""
    plain = Part.Call("c1", "bash", '{"command": "x"}')
    tricky = Part.Call("c2", "bash", json.dumps({"command": "x\n(ask rule: y)\nz"}))
    update = asked("bash: x\n(ask rule: y)\nz\n(ask rule: bash)", plain, tricky)
    assert update["toolCallId"] == "question:0"


def png(width: int, height: int) -> bytes:
    """A valid PNG of ``width`` x ``height`` black pixels."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + b"\0" * (3 * width) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_images_in_a_result_are_a_line_each_after_its_text():
    images = [Image(png(3, 2)), Image(png(1, 1))]
    update = translate.outcome_update(3, "c1", Outcome.Ok("two pictures", images))
    shown = "two pictures\n[image 3x2 image/png]\n[image 1x1 image/png]"
    assert update["status"] == "completed"
    assert update["content"] == [{"type": "content", "content": {"type": "text", "text": shown}}]
    assert update["rawOutput"] == shown
    assert translate.ok_text(Outcome.Ok("no pictures")) == "no pictures"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_image_blocks_become_image_inputs_after_the_text():
    data = b64(png(2, 3))
    blocks = [
        block(type="image", data=data, mimeType="image/png"),
        block(type="text", text="what is this"),
    ]
    text, images = translate.prompt_content(blocks)
    assert text == "what is this"
    assert [i.data for i in images] == [data]


def test_an_image_alone_has_no_text():
    blocks = [block(type="image", data=b64(png(1, 1)), mimeType="image/png")]
    assert translate.prompt_content(blocks)[0] == ""


@pytest.mark.parametrize("data", ["AA", "not base64!", None])
def test_an_image_tau_would_refuse_is_refused_here(data):
    with pytest.raises(ValueError, match="image"):
        translate.prompt_content([block(type="image", data=data, mimeType="image/png")])


def test_a_user_message_with_images_replays_text_then_image_blocks():
    image = Image(png(3, 2))
    node = Node(1, Body.User("look", [image]), 0)
    assert translate.image_hashes([node]) == {image.sha256}
    updates = translate.replay_updates([node], "/w", {image.sha256: "QUJD"})
    assert updates == [
        translate.chunk("user_message_chunk", "look"),
        {
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "image", "data": "QUJD", "mimeType": "image/png"},
        },
    ]


def test_an_image_only_message_replays_no_empty_text_chunk():
    image = Image(png(1, 1))
    updates = translate.user_updates(Body.User("", [image]), {image.sha256: "QUJD"})
    assert [u["content"]["type"] for u in updates] == ["image"]


def test_a_notice_is_a_chat_line_with_its_extension_and_level():
    assert translate.notice_text(Notice("hi")) == "\n[τ] hi\n"
    warned = Notice("slow", extension="usage", level="warning")
    assert translate.notice_text(warned) == "\n[usage] warning: slow\n"
    assert translate.notice_update(warned, native=False)["sessionUpdate"] == "agent_message_chunk"


def test_a_notice_is_acps_notice_when_the_editor_takes_it():
    update = translate.notice_update(
        Notice("slow", extension="usage", level="warning"), native=True
    )
    assert update == {"sessionUpdate": "notice", "severity": "warning", "title": "[usage] slow"}
    long = translate.notice_update(Notice("first\nmore"), native=True)
    assert long["title"] == "[τ] first"
    assert long["description"] == "first\nmore"


def reply(meta):
    return Node(5, Body.Assistant([Part.Text("x")], Stop.End, None, meta), 4)


def test_usage_update_is_the_replys_prompt_and_output_in_the_context_window():
    node = reply(json.dumps({"usage": {"input": 100, "output": 20, "cached": 50}}))
    assert translate.usage_update(node, 8000) == {
        "sessionUpdate": "usage_update",
        "used": 120,
        "size": 8000,
    }


def test_no_usage_update_without_usage_or_a_window():
    usage = json.dumps({"usage": {"input": 1, "output": 1}})
    assert translate.usage_update(reply(None), 8000) is None
    assert translate.usage_update(reply(json.dumps({"id": "x"})), 8000) is None
    assert translate.usage_update(reply(usage), None) is None


def test_a_usage_without_counts_is_an_error():
    with pytest.raises(ValueError, match="input and output"):
        translate.usage_update(reply(json.dumps({"usage": {"input": "many"}})), 8000)


@pytest.mark.parametrize(
    ("schema", "prop"),
    [
        ({"type": "string"}, {"type": "string"}),
        ({"type": "boolean"}, {"type": "boolean"}),
        ({"type": "integer"}, {"type": "integer"}),
        ({"enum": ["a", "b"]}, {"type": "string", "enum": ["a", "b"]}),
        ({"type": "array", "items": {"type": "string"}}, None),
        ({"enum": [1, 2]}, None),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, None),
    ],
)
def test_elicitation_property(schema, prop):
    assert translate.elicitation_property(schema) == prop


def test_elicitation_params_ask_one_field_named_answer():
    question = OpenQuestion(7, None, "x", "Proceed?", {"type": "boolean"})
    params = translate.elicitation_params(question, {"type": "boolean"})
    assert params["mode"] == "form"
    assert params["message"] == "Proceed?"
    schema = params["requestedSchema"]
    assert schema["properties"] == {"answer": {"type": "boolean", "title": "Answer"}}
    assert schema["required"] == ["answer"]


@pytest.mark.parametrize(
    ("result", "answer"),
    [
        ({"action": "accept", "content": {"answer": True}}, True),
        ({"action": "accept", "content": {"answer": "x"}}, "x"),
        ({"action": "decline"}, None),
        ({"action": "cancel"}, None),
        ({"action": "accept"}, None),
        ("nonsense", None),
        (None, None),
    ],
)
def test_elicitation_answer(result, answer):
    assert translate.elicitation_answer(result) == answer
