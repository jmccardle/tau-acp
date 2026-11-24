"""A τ extension for the tests: a user message of the form ``call TOOL {json args}`` is answered
by a drafted reply that makes that call, as a model would. Loaded with ``-e`` through tau-acp's
serve arguments. Any other message is left to the ``mock`` provider."""

import json
import re

from tau import Author, Body, Draft, Part, Stop

CALL = re.compile(r"call (\w+) (\{.*\})", re.DOTALL)


class Scripted:
    name = "acpscripted"

    async def pre_sample(self, cursor, subject):
        last = cursor.walk()[-1]
        if not isinstance(last.body, Body.User):
            return None
        found = CALL.fullmatch(last.body.text.strip())
        if found is None:
            return None
        tool, args = found.groups()
        parts = [Part.Text("calling"), Part.Call("c1", tool, json.dumps(json.loads(args)))]
        subject.draft = Draft(Body.Assistant(parts, Stop.Tools), [], Author.Provider())
        return None


extensions = [Scripted()]
