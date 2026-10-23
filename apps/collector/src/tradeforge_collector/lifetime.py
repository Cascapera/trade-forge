"""The agent's lifetime settings, in a module with no dependencies.

Split from `supervisor` so the CLI can show the default in `--help` without importing arq and
redis for every subcommand.
"""

DEFAULT_GRACE = 15.0
"""Seconds Redis may go unanswered before the agent concludes the stack is down. Long enough for
a container restart; short enough that a stopped stack does not leave the agent behind."""

EXIT_NO_REDIS = 2
"""The agent's status when Redis did not answer at start: a stack that is not up, or a wrong
REDIS_HOST/REDIS_PORT — a setup to fix, unlike a stack that went away later, which is 0."""
