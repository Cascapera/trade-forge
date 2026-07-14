"""What the engine refuses to do.

Each of these is an invariant from AGENTS.md §5 with a stack trace attached. They are
errors rather than warnings because every one of them, allowed through, produces a
*plausible* backtest — a result that looks right, ranks well, and is false.
"""


class EngineError(Exception):
    """Base class for every refusal the engine makes."""


class LookaheadError(EngineError):
    """A fill used information the strategy could not have had.

    The engine checks this on every fill, whatever broker produced it. That is the point:
    the rule is not "our broker is careful", it is "no broker gets to break this" — and
    the broker written next year, by someone who never read the ADRs, is exactly the one
    this is for.
    """
