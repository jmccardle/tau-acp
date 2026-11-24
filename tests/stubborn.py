"""A τ extension for the tests: a tool that sleeps in a thread, which nothing can interrupt, so
that a serve told to stop has a process that outlives its stream."""

import time


def stubborn() -> str:
    """Sleep for a while, deaf to cancellation."""
    time.sleep(8)
    return "done"


class Stubborn:
    name = "acpstubborn"
    tools = (stubborn,)


extensions = [Stubborn()]
