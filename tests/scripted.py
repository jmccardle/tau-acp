"""A τ extension for the tests: a user message of lines of the form ``call TOOL {json args}`` is
answered by a drafted reply that makes those calls (``c1``, ``c2``, ...) as one batch, as a model
would. Loaded with ``-e`` through tau-acp's
serve arguments. Any other message is left to the ``mock`` provider."""

import json
import re

from tau import Author, Body, Draft, Part, Stop

CALL = re.compile(r"call (\w+) (\{.*\})")


class Scripted:
    name = "acpscripted"

    async def pre_sample(self, cursor, subject):
        last = cursor.walk()[-1]
        if not isinstance(last.body, Body.User):
            return None
        lines = last.body.text.strip().splitlines()
        found = [CALL.fullmatch(line) for line in lines]
        if not found or not all(found):
            return None
        calls = [
            Part.Call(f"c{n}", tool, json.dumps(json.loads(args)))
            for n, (tool, args) in enumerate((f.groups() for f in found), 1)
        ]
        parts = [Part.Text("calling"), *calls]
        subject.draft = Draft(Body.Assistant(parts, Stop.Tools), [], Author.Provider())
        return None


extensions = [Scripted()]
