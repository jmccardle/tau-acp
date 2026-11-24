"""Pure translation between ACP values and τ values: no IO, no state.

Updates are the ``update`` member of a ``session/update`` notification, as plain dicts.
"""

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tau import Author, Body, CursorView, Ending, Form, Node, OpenQuestion, Outcome, Part, Stop

Update = dict[str, Any]

TITLE_WIDTH = 80
STREAM_BREAK = "[tau-acp: the streamed text was incomplete; the final text follows]"
KINDS = {
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "grep": "search",
    "find": "search",
    "bash": "execute",
}
PATH_TOOLS = ("read", "write", "edit")
PERMISSION_OPTIONS = (
    ("yes", "Allow once", "allow_once"),
    ("always", "Always allow", "allow_always"),
    ("no", "Reject", "reject_once"),
)


class TurnError(Exception):
    """A turn that ended in a way ACP has no stop reason for: it answers with -32603."""


# Prompt content


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _block_text(block: Mapping[str, Any]) -> str:
    kind = block.get("type")
    if kind == "text":
        return str(block["text"])
    if kind == "resource_link":
        return f"[{block['name']}]({block['uri']})"
    if kind == "resource":
        resource = block["resource"]
        if "text" not in resource:
            raise ValueError("a binary embedded resource cannot be sent to τ: it takes text")
        fence = _fence(resource["text"])
        return f"{resource['uri']}\n{fence}\n{resource['text']}\n{fence}"
    raise ValueError(f"a {kind!r} content block cannot be sent to τ: it takes text")


def prompt_text(blocks: Sequence[Mapping[str, Any]]) -> str:
    """The prompt's blocks as one text, blocks separated by a blank line. ``ValueError`` for a
    block τ cannot take (image, audio, a binary resource) or an empty prompt."""
    if not blocks:
        raise ValueError("the prompt is empty")
    return "\n\n".join(_block_text(block) for block in blocks)


# Updates


def chunk(kind: str, text: str) -> Update:
    """A text content chunk: ``kind`` is ``agent_message_chunk`` and the like."""
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def tool_kind(tool: str) -> str:
    """The ACP tool kind of a τ tool."""
    return KINDS.get(tool, "other")


def parse_args(args_raw: str) -> Any:
    """The call's arguments as JSON, or the raw text when they do not parse."""
    try:
        return json.loads(args_raw)
    except ValueError:
        return args_raw


def _cut(text: str) -> str:
    text = text.strip().splitlines()[0] if text.strip() else ""
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1] + "…"


def tool_title(tool: str, args_raw: str) -> str:
    """``<tool>`` plus a short summary: the path, the command, or the arguments cut to 80."""
    args = parse_args(args_raw)
    detail = None
    if isinstance(args, dict):
        detail = next(
            (args[key] for key in ("path", "command") if isinstance(args.get(key), str)), None
        )
    if detail is None:
        detail = args_raw
    summary = _cut(detail)
    return f"{tool}: {summary}" if summary else tool


def tool_locations(tool: str, args_raw: str, cwd: str) -> list[dict[str, str]]:
    """The file a path tool works on, resolved against ``cwd``; none for other tools."""
    args = parse_args(args_raw)
    if not isinstance(args, dict) or not isinstance(args.get("path"), str):
        return []
    if tool not in (*PATH_TOOLS, "grep", "find"):
        return []
    return [{"path": os.path.normpath(os.path.join(cwd, args["path"]))}]


def call_id(node: int, call: str) -> str:
    """The ACP tool call id of the call ``call`` in the Assistant node ``node``: a provider's
    call ids can repeat across replies."""
    return f"{node}:{call}"


def tool_call(node: int, call: Any, cwd: str, status: str = "pending") -> Update:
    """The ``tool_call`` update for a ``Part.Call`` of the Assistant node ``node``."""
    update: Update = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id(node, call.id),
        "title": tool_title(call.tool, call.args_raw),
        "kind": tool_kind(call.tool),
        "status": status,
        "rawInput": parse_args(call.args_raw),
    }
    if locations := tool_locations(call.tool, call.args_raw, cwd):
        update["locations"] = locations
    return update


def tool_text(identity: str, status: str, text: str) -> Update:
    """A ``tool_call_update`` carrying text output."""
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": identity,
        "status": status,
        "content": [{"type": "content", "content": {"type": "text", "text": text}}],
        "rawOutput": text,
    }


def tool_progress(identity: str, text: str) -> Update:
    """A ``tool_call_update`` for a running call, with its output so far."""
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": identity,
        "status": "in_progress",
        "content": [{"type": "content", "content": {"type": "text", "text": text}}],
    }


def outcome_update(node: int, call: str, outcome: Any) -> Update:
    """The ``tool_call_update`` for the outcome of a call."""
    identity = call_id(node, call)
    if isinstance(outcome, Outcome.Ok):
        return tool_text(identity, "completed", outcome.value)
    if isinstance(outcome, Outcome.Err):
        return tool_text(identity, "failed", outcome.error)
    return tool_text(identity, "failed", "interrupted")


def results_updates(node: Node) -> list[Update]:
    """The updates for a Results node, one per outcome."""
    assert isinstance(node.body, Body.Results)
    if node.parent is None:
        raise ValueError(f"results node {node.id} has no parent: no call to attach them to")
    return [outcome_update(node.parent, call, outcome) for call, outcome, _by in node.body.outcomes]


def unstreamed(full: str, seen: str) -> tuple[str, bool]:
    """What of ``full`` was not streamed as ``seen``, and whether ``seen`` was not a prefix of it
    (in which case all of ``full`` is returned)."""
    if full.startswith(seen):
        return full[len(seen) :], False
    return full, True


def assistant_updates(
    node: Node, cwd: str, text_seen: str = "", thought_seen: str = ""
) -> list[Update]:
    """The updates for an Assistant node. ``text_seen`` and ``thought_seen`` are what was already
    streamed as deltas: only the rest is sent."""
    assert isinstance(node.body, Body.Assistant)
    parts = node.body.parts
    text = "".join(p.text for p in parts if isinstance(p, Part.Text))
    thought = "".join(p.text for p in parts if isinstance(p, Part.Thinking))
    updates: list[Update] = []
    sent: set[str] = set()
    for part in parts:
        if isinstance(part, Part.Call):
            updates.append(tool_call(node.id, part, cwd))
        elif isinstance(part, Part.Text) and "text" not in sent:
            sent.add("text")
            updates += _remainder("agent_message_chunk", text, text_seen, STREAM_BREAK)
        elif isinstance(part, Part.Thinking) and "thought" not in sent:
            sent.add("thought")
            updates += _remainder("agent_thought_chunk", thought, thought_seen, None)
    return updates


def _remainder(kind: str, full: str, seen: str, marker: str | None) -> list[Update]:
    rest, broken = unstreamed(full, seen)
    if broken and marker:
        rest = f"{marker}\n{rest}"
    return [chunk(kind, rest)] if rest else []


def replay_updates(nodes: Iterable[Node], cwd: str) -> list[Update]:
    """The updates that replay a walk: users, assistants part by part, and results."""
    updates: list[Update] = []
    for node in nodes:
        body = node.body
        if isinstance(body, Body.User):
            updates.append(chunk("user_message_chunk", body.text))
        elif isinstance(body, Body.Assistant):
            updates += assistant_updates(node, cwd)
        elif isinstance(body, Body.Results):
            updates += results_updates(node)
    return updates


def is_own(node: Node, client: str) -> bool:
    """Whether the node is input this client wrote."""
    return isinstance(node.by, Author.Frontend) and node.by.client == client


# Commands


def command_names(forms: Sequence[Form]) -> dict[str, Form]:
    """The slash name for each command: its name, or ``extension:name`` when two share it."""
    counts: dict[str, int] = {}
    for form in forms:
        counts[form.name] = counts.get(form.name, 0) + 1
    return {(f.name if counts[f.name] == 1 else f.qualified): f for f in forms}


def _hint(form: Form) -> str | None:
    properties = form.schema.get("properties", {})
    required = set(form.schema.get("required", ()))
    names = [f"<{n}>" if n in required else f"[{n}]" for n in properties]
    return " ".join(names) or None


def available_commands(forms: Sequence[Form]) -> Update:
    """The ``available_commands_update`` for τ's commands."""
    commands = []
    for name, form in command_names(forms).items():
        command: dict[str, Any] = {"name": name, "description": form.description}
        if hint := _hint(form):
            command["input"] = {"hint": hint}
        commands.append(command)
    return {"sessionUpdate": "available_commands_update", "availableCommands": commands}


# Permissions


def permission_options() -> list[dict[str, str]]:
    """The options of a ``session/request_permission`` for τ's yes / always / no."""
    return [{"optionId": i, "name": n, "kind": k} for i, n, k in PERMISSION_OPTIONS]


def permission_answer(result: Any) -> str | None:
    """τ's answer for the client's response, or None (decline) for a cancelled, malformed or
    unknown outcome."""
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
        return None
    chosen = outcome.get("optionId")
    return chosen if chosen in {option[0] for option in PERMISSION_OPTIONS} else None


def permission_tool_call(
    question: OpenQuestion, node: Node | None, taken: set[str], cwd: str
) -> Update:
    """The tool call a permissions question is about: the first call in the Assistant node
    ``node`` whose tool is the text before ``": "`` and that is not in ``taken`` (the ids of calls
    asked about already; the match is added to it). Without one, a call made of the question."""
    tool = question.text.split(": ", 1)[0]
    if node is not None and isinstance(node.body, Body.Assistant):
        for part in node.body.parts:
            identity = call_id(node.id, part.id) if isinstance(part, Part.Call) else None
            if identity and part.tool == tool and identity not in taken:
                taken.add(identity)
                update = tool_call(node.id, part, cwd)
                del update["sessionUpdate"], update["status"]
                return update
    return {"toolCallId": f"question:{question.id}", "title": question.text}


# Stop reasons


def stop_reason(view: CursorView, last_stop: Stop | None, *, message: bool, cancelled: bool) -> str:
    """The ACP stop reason for the cursor's resting view. ``last_stop`` is the stop of the turn's
    last Assistant node, ``message`` whether the turn sent input (which must have been taken up),
    ``cancelled`` whether the client cancelled it. ``TurnError`` for an ending ACP cannot carry."""
    if cancelled:
        return "cancelled"
    ending = view.ending
    if isinstance(ending, Ending.Aborted):
        return "cancelled"
    if isinstance(ending, Ending.Failed):
        raise TurnError(ending.error)
    if isinstance(ending, Ending.Halted):
        raise TurnError(f"halted: {ending.reason}")
    if ending is None:
        raise TurnError("the cursor rested with no recorded ending")
    if message and view.items:
        raise TurnError("stopped before finishing: the message is still pending")
    if message and not view.auto:
        raise TurnError(
            "stopped before finishing: auto-step is off "
            "(a step limit, a hook, or a step that did not move)"
        )
    return _stop(last_stop, message=message)


def _stop(last_stop: Stop | None, *, message: bool) -> str:
    if last_stop is None:
        if message:
            raise TurnError("the turn recorded no reply")
        return "end_turn"
    reasons = ((Stop.End, "end_turn"), (Stop.Length, "max_tokens"), (Stop.Refused, "refusal"))
    for stop, reason in reasons:
        if last_stop == stop:
            return reason
    raise TurnError(f"the last reply ended with {last_stop!r}")
