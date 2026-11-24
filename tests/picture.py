"""A τ extension for the tests: a user message ``show PATH`` is answered by a reply that calls
``read`` on PATH, and the result by a reply ``seen``, so no provider is ever asked for a sample. τ
sends an image a tool returns only to a model that takes images, on a provider that declares
image limits, which the ``mock`` provider does not: the tests that load this extension name an
``openai`` model whose server is never contacted. Loaded with ``-e`` through tau-acp's serve
arguments."""

import json

from tau import Author, Body, Draft, Part, Stop


class Picture:
    name = "acppicture"

    async def pre_sample(self, cursor, subject):
        line = cursor.walk()
        users = [node for node in line if isinstance(node.body, Body.User)]
        if not users or not users[-1].body.text.startswith("show "):
            return None
        last = line[-1].body
        if isinstance(last, Body.User):
            path = last.text.removeprefix("show ").strip()
            parts = [Part.Call("c1", "read", json.dumps({"path": path}))]
            subject.draft = Draft(Body.Assistant(parts, Stop.Tools), [], Author.Provider())
        elif isinstance(last, Body.Results):
            parts = [Part.Text("seen")]
            subject.draft = Draft(Body.Assistant(parts, Stop.End), [], Author.Provider())
        return None


extensions = [Picture()]
