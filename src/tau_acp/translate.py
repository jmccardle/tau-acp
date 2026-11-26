"""Pure translation between ACP values and τ values: no IO, no state.

Updates are the ``update`` member of a ``session/update`` notification, as plain dicts.
"""

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tau import (
    Author,
    Body,
    CursorView,
    Ending,
    Form,
    ImageInput,
    Node,
    Notice,
    OpenQuestion,
    Outcome,
    Part,
    Stop,
)

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


def _block_text(block: Mapping[str, Any]) -> str | None:
    """The text a block contributes, or None for an image (which goes as an image)."""
    if not isinstance(block, Mapping):
        raise ValueError(f"a prompt content block must be an object, not {block!r:.40}")
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
    if kind == "image":
        return None
    raise ValueError(f"a {kind!r} content block cannot be sent to τ: it takes text and images")


def _image(block: Mapping[str, Any]) -> ImageInput:
    data = block.get("data")
    if not isinstance(data, str):
        raise ValueError("an image content block needs its bytes as base64 `data`")
    try:
        return ImageInput(data=data)  # τ checks the bytes: strict base64 of a PNG/JPEG/GIF/WebP
    except ValueError as error:  # BadImage
        raise ValueError(f"an image content block τ cannot take: {error}") from None


def prompt_content(blocks: Sequence[Mapping[str, Any]]) -> tuple[str, list[ImageInput]]:
    """The prompt as τ takes it: its text blocks as one text, separated by a blank line, and its
    image blocks as τ's image inputs, in order (τ puts a message's images after its text).
    ``ValueError`` for a block τ cannot take (audio, a binary resource, an image that is not
    PNG, JPEG, GIF or WebP) or an empty prompt."""
    if not blocks:
        raise ValueError("the prompt is empty")
    texts = [t for block in blocks if (t := _block_text(block)) is not None]
    images = [_image(block) for block in blocks if block.get("type") == "image"]
    return "\n\n".join(texts), images


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


def ok_text(outcome: Outcome.Ok) -> str:
    """A result's text, then a line per image it returned, as τ's own TUI shows them:
    ``[image WxH mime]``. τ's wire carries an image's reference, not its bytes, so it cannot be
    sent as an ACP image block."""
    lines = [f"[image {i.width}x{i.height} {i.mime}]" for i in outcome.images]
    return "\n".join([outcome.value, *lines]) if lines else outcome.value


def outcome_update(node: int, call: str, outcome: Any) -> Update:
    """The ``tool_call_update`` for the outcome of a call."""
    identity = call_id(node, call)
    if isinstance(outcome, Outcome.Ok):
        return tool_text(identity, "completed", ok_text(outcome))
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


def image_hashes(nodes: Iterable[Node]) -> set[str]:
    """The sha256 of every image the User nodes carry: the bytes ``replay_updates`` needs."""
    return {image.sha256 for n in nodes if isinstance(n.body, Body.User) for image in n.body.images}


def user_updates(body: Body.User, images: Mapping[str, str]) -> list[Update]:
    """The updates for a user's message: its text, then an ``image`` block per image. ``images``
    maps each image's sha256 to its bytes as base64 (τ's ``Client.image``)."""
    updates = [chunk("user_message_chunk", body.text)] if body.text or not body.images else []
    for image in body.images:
        content = {"type": "image", "data": images[image.sha256], "mimeType": image.mime}
        updates.append({"sessionUpdate": "user_message_chunk", "content": content})
    return updates


def replay_updates(nodes: Iterable[Node], cwd: str, images: Mapping[str, str]) -> list[Update]:
    """The updates that replay a walk: users, assistants part by part, and results. ``images``
    holds the bytes of ``image_hashes(nodes)``."""
    updates: list[Update] = []
    for node in nodes:
        body = node.body
        if isinstance(body, Body.User):
            updates += user_updates(body, images)
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


# Notices


def notice_text(notice: Notice) -> str:
    """A notice as chat text: ``[extension] text``, or ``[τ] text`` when τ itself said it, with
    the level named when it is not ``info``."""
    who = notice.extension or "τ"
    level = "" if notice.level == "info" else f"{notice.level}: "
    return f"\n[{who}] {level}{notice.text}\n"


def notice_update(notice: Notice, *, native: bool) -> Update:
    """The update for a notice: ACP's ``notice`` (severity, title, description) when the client
    advertised it, else a message chunk."""
    if not native:
        return chunk("agent_message_chunk", notice_text(notice))
    first, _, rest = notice.text.strip().partition("\n")
    title = f"[{notice.extension or 'τ'}] {_cut(first)}"
    update: Update = {"sessionUpdate": "notice", "severity": notice.level, "title": title}
    if rest.strip() or len(first) > TITLE_WIDTH:
        update["description"] = notice.text
    return update


# Usage


def usage_update(node: Node, window: int | None) -> Update | None:
    """ACP's ``usage_update`` for an Assistant node: ``used`` is the reply's prompt and output
    tokens (τ's own ``ctx`` rule), ``size`` the model's ``context_window``. None when the reply
    reported no usage or the model has no context window: ``size`` is required, and unknown.
    ``ValueError`` when ``meta.usage`` is not the shape τ documents."""
    body = node.body
    assert isinstance(body, Body.Assistant)
    if body.meta is None or window is None:
        return None
    meta = json.loads(body.meta)
    usage = meta.get("usage") if isinstance(meta, dict) else None
    if usage is None:
        return None
    counts = [usage.get(key) if isinstance(usage, dict) else None for key in ("input", "output")]
    if not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0 for c in counts):
        raise ValueError(f"node {node.id}: meta.usage has no input and output counts: {usage!r}")
    return {"sessionUpdate": "usage_update", "used": sum(counts), "size": window}


# Questions


def elicitation_property(schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ACP form property for the JSON Schema of a τ question's answer, or None when a form
    cannot ask it: a string, a boolean, an integer, a number, or one of some strings."""
    choices = schema.get("enum")
    if isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices):
        return {"type": "string", "enum": choices}
    kind = schema.get("type")
    if kind in ("string", "boolean", "integer", "number") and set(schema) <= {"type", "title"}:
        return {"type": kind}
    return None


def elicitation_params(question: OpenQuestion, prop: Mapping[str, Any]) -> dict[str, Any]:
    """The params of an ``elicitation/create`` (form mode) that asks a τ question: one field,
    ``answer``."""
    field = {**prop, "title": "Answer"}
    return {
        "mode": "form",
        "message": question.text,
        "requestedSchema": {
            "type": "object",
            "properties": {"answer": field},
            "required": ["answer"],
        },
    }


def elicitation_answer(result: Any) -> object | None:
    """τ's answer for the client's response: the ``answer`` field of an accepted form, else None
    (decline: ``decline``, ``cancel``, or anything malformed)."""
    if not isinstance(result, dict) or result.get("action") != "accept":
        return None
    content = result.get("content")
    return content.get("answer") if isinstance(content, dict) else None


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


# What τ's permissions extension writes before "\n(ask rule: ...": `Call.shown` in τ's
# `_permissions.py`. A question carries no call id, so the call it asks about is found by
# building this text for every call of the batch and comparing.
ASK_MARKER = "\n(ask rule: "
SHOWN_PATH_TOOLS = ("read", "write", "edit", "grep", "find")


def _named_paths(tool: str, args: Any) -> tuple[str, ...]:
    """The paths a path tool's arguments name (τ's ``_paths``); none if they are not strings."""
    if not isinstance(args, dict):
        return ()
    named: list[Any] = []
    if "path" in args:
        named.append(args["path"])
    files = args.get("files") if tool == "grep" else None
    if files is not None:
        if not isinstance(files, list):
            return ()
        named.extend(files)
    if not named and tool in ("grep", "find"):
        named.append(".")
    return tuple(named) if all(isinstance(n, str) for n in named) else ()


def shown(tool: str, args_raw: str, cwd: str) -> str:
    """The text τ's permissions question shows for a call: a line per resolved path for a path
    tool, ``bash: <command>``, or ``<tool>: <args_raw>``."""
    parsed = parse_args(args_raw)
    args = parsed if isinstance(parsed, dict) else {}
    if tool in SHOWN_PATH_TOOLS and (paths := _named_paths(tool, args)):
        return "\n".join(f"{tool}: {os.path.realpath(os.path.join(cwd, p))}" for p in paths)
    command = args.get("command") if tool == "bash" else None
    return f"{tool}: {command if isinstance(command, str) else args_raw}"


def permission_tool_call(
    question: OpenQuestion, node: Node | None, taken: set[str], cwd: str
) -> Update:
    """The tool call a permissions question is about, as the ``toolCall`` of a
    ``session/request_permission``, always with the question's text as its content.

    The call is the one in the Assistant node ``node`` whose ``shown`` text is the question's
    (the question is that text, then ``(ask rule: ...)``) and whose id is not in ``taken`` (the
    ids of calls asked about already; the match is added to it). Calls with the same text are
    taken in turn. When none matches, or the question is a prefix of more than one different
    text (a command can contain the marker), the call is not guessed: ``question:<id>`` with the
    question's text as title."""
    said = [{"type": "content", "content": {"type": "text", "text": question.text}}]
    if node is not None and isinstance(node.body, Body.Assistant):
        found = [
            (part, text)
            for part in node.body.parts
            if isinstance(part, Part.Call)
            and call_id(node.id, part.id) not in taken
            and question.text.startswith(
                (text := shown(part.tool, part.args_raw, cwd)) + ASK_MARKER
            )
        ]
        if found and len({text for _part, text in found}) == 1:
            part = found[0][0]
            taken.add(call_id(node.id, part.id))
            update = tool_call(node.id, part, cwd)
            del update["sessionUpdate"], update["status"]
            return {**update, "content": said}
    return {"toolCallId": f"question:{question.id}", "title": question.text, "content": said}


# Stop reasons


def stop_reason(
    view: CursorView,
    last_stop: Stop | None,
    *,
    message: bool,
    cancelled: bool,
    limited: bool = False,
) -> str:
    """The ACP stop reason for the cursor's resting view. ``last_stop`` is the stop of the turn's
    last Assistant node, ``message`` whether the turn sent input (which must have been taken up),
    ``cancelled`` whether the client cancelled it, ``limited`` whether τ's step limit stopped it
    (``max_turn_requests``). ``TurnError`` for an ending ACP cannot carry."""
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
    if limited:
        return "max_turn_requests"
    if message and not view.auto:
        raise TurnError(
            "stopped before finishing: auto-step is off (a hook, or a step that did not move)"
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
