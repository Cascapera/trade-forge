"""Saying once that Redis or the database went away, and saying when they came back (13/10).

The signals supervisor and the notifier are the two processes that outlive everything else on the
Windows host, and on 12/10 both died with the stack: a `wsl --shutdown` took Postgres and Redis
down for a few minutes, an unguarded read raised, and nobody was left to restart the sessions or
to post their signals until somebody noticed. A lost round is now just that — logged, waited out,
tried again.

⚠️ **Once, not every round.** A round lasts 5 to 15 s and an outage lasts minutes, so logging every
failure with its traceback buries the one line that says what happened under dozens that say the
same thing. The first failure carries the traceback; the rest are a count; the recovery says how
long it took.
"""

import logging
from dataclasses import dataclass

__all__ = ["Outage"]


@dataclass(slots=True)
class Outage:
    """The current run of lost rounds, if any."""

    logger: logging.Logger
    rounds: int = 0

    def lost(self, what: str) -> None:
        """A round failed at `what`. Call it from inside the `except`, so the first one can carry
        the traceback."""
        self.rounds += 1
        if self.rounds == 1:
            self.logger.exception("%s failed; trying again every round until it answers", what)
        else:
            self.logger.warning("%s still failing (%d rounds in a row)", what, self.rounds)

    def back(self) -> None:
        """A round went through: end the run, saying so when there was one."""
        if self.rounds:
            self.logger.info("back after %d lost round(s)", self.rounds)
        self.rounds = 0
