"""A τ extension for the tests, for what an extension can say to a client besides tool calls.

A user message ``ask bool``, ``ask str`` or ``ask pick`` puts a question (a yes/no, a text, or one
of ``red`` and ``blue``) and is answered by a reply that says what the answer was, ``got <repr>``;
``ask list`` asks for a list of strings, which ACP has no form for. A message ``warn`` tells the
client a warning and an error, as notices, and is then answered by ``warned``. The command
``/grow`` adds a command, ``/grown``, to the extension, as one that came with a server would.
Loaded with ``-e`` through tau-acp's serve arguments."""

from typing import Literal

from tau import Author, Body, Cursor, Draft, Part, Stop

KINDS = {"bool": bool, "str": str, "pick": Literal["red", "blue"], "list": list[str]}


class Asking:
    name = "acpasking"

    def __init__(self):
        self.commands = (self.grow,)
        self._tree = None

    async def start(self, tree):
        self._tree = tree

    async def grow(self, cursor: Cursor):
        """Add the command /grown."""
        self.commands = (self.grow, self.grown)
        self._tree.reassemble()

    async def grown(self, cursor: Cursor):
        """The command that /grow added."""
        return "grown"

    async def pre_sample(self, cursor, subject):
        last = cursor.walk()[-1]
        if not isinstance(last.body, Body.User):
            return None
        text = last.body.text.strip()
        if text.startswith("ask "):
            answer = await cursor.ask(f"Which {text}?", KINDS[text.removeprefix("ask ")])
            reply = f"got {answer!r}"
        elif text == "warn":
            cursor.tell("careful", level="warning")
            cursor.tell("broken", level="error")
            reply = "warned"
        else:
            return None
        parts = [Part.Text(reply)]
        subject.draft = Draft(Body.Assistant(parts, Stop.End), [], Author.Provider())
        return None


extensions = [Asking()]
