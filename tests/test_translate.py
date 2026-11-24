"""The pure translation functions, on τ's public value types."""

import pytest
from tau import Author, Body, CursorView, Ending, Form, Outcome, Part, Stop

from tau_acp import translate


def block(**fields):
    return fields


def test_prompt_text_joins_blocks_with_a_blank_line():
    blocks = [
        block(type="text", text="look at this"),
        block(type="resource_link", name="a.py", uri="file:///a.py"),
        block(type="resource", resource={"uri": "file:///b.py", "text": "x = 1"}),
    ]
    assert translate.prompt_text(blocks) == (
        "look at this\n\n[a.py](file:///a.py)\n\nfile:///b.py\n```\nx = 1\n```"
    )


def test_embedded_text_gets_a_fence_longer_than_its_backticks():
    resource = {"uri": "u", "text": "```py\nx\n```"}
    assert translate.prompt_text([block(type="resource", resource=resource)]) == (
        "u\n````\n```py\nx\n```\n````"
    )


@pytest.mark.parametrize(
    "bad",
    [
        block(type="image", data="AA", mimeType="image/png"),
        block(type="audio", data="AA", mimeType="audio/wav"),
        block(type="resource", resource={"uri": "u", "blob": "AA"}),
        block(type="mystery"),
    ],
)
def test_prompt_text_refuses_what_tau_cannot_take(bad):
    with pytest.raises(ValueError, match=r"cannot be sent|binary"):
        translate.prompt_text([bad])


def test_prompt_text_refuses_an_empty_prompt():
    with pytest.raises(ValueError, match="empty"):
        translate.prompt_text([])


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
    updates = translate.replay_updates(nodes, "/w")
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


def test_an_unusable_last_stop_is_an_error():
    with pytest.raises(translate.TurnError, match="no reply"):
        translate.stop_reason(view(Ending.Done()), None, message=True, cancelled=False)
    with pytest.raises(translate.TurnError, match="ended with"):
        translate.stop_reason(view(Ending.Done()), Stop.Error, message=True, cancelled=False)
