"""A τ extension for the tests, for the ways a turn that keeps calling tools ends. A user message
``loop`` is answered by a reply that calls ``read`` on ``loop.txt``, and so is every result after
it, as a model that never stops calling tools would: only τ's step limit ends such a turn. A user
message ``pause`` starts the same, but this extension's ``post_step`` turns auto-step off after
the first step, as a hook may. Loaded with ``-e`` through tau-acp's serve arguments."""

import json

from tau import Author, Body, Draft, Part, Stop


def _asked(line):
    users = [node for node in line if isinstance(node.body, Body.User)]
    return users[-1].body.text.strip() if users else None


class Looping:
    name = "acplooping"

    async def pre_sample(self, cursor, subject):
        line = cursor.walk()
        if _asked(line) not in ("loop", "pause"):
            return None
        if not isinstance(line[-1].body, (Body.User, Body.Results)):
            return None
        call = Part.Call(f"c{len(line)}", "read", json.dumps({"path": "loop.txt"}))
        subject.draft = Draft(Body.Assistant([call], Stop.Tools), [], Author.Provider())
        return None

    async def post_step(self, cursor, subject):
        if _asked(cursor.walk()) == "pause":
            cursor.set_auto(False)


extensions = [Looping()]
