"""Docker has to wait longer for a stopping worker than the worker waits for its job.

⚠️ **Two numbers in two places, and only their order matters** (16/10, ADR-0036). A worker told
to stop finishes the job it holds for up to `STOP_WAIT_SECONDS` (`job_completion_wait`) and
cancels it only after that. Docker sends that SIGTERM and kills the container `stop_grace_period`
later. If the grace ran out first, the kill would land in the middle of the batch: no Python runs
after a SIGKILL, the unwritten runs stay `running` until arq's lease on the job lapses two hours
later, and a machine scaling its workers down through the day would leave a trail of them.

Nothing ties the compose files to the Python constant, so this test does.
"""

import re
from pathlib import Path

import pytest
import yaml

from tradeforge_api.worker import STOP_WAIT_SECONDS, WorkerSettings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.worker.yml")

_DURATION = re.compile(r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$")


def seconds(duration: str) -> int:
    """A compose duration (`15m`, `1h30m`, `90s`) in seconds."""
    match = _DURATION.match(duration.strip())
    if match is None or not any(match.groupdict().values()):
        raise ValueError(f"not a duration this test reads: {duration!r}")
    parts = {unit: int(value or 0) for unit, value in match.groupdict().items()}
    return parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def test_the_worker_waits_for_its_job_when_told_to_stop() -> None:
    assert WorkerSettings.job_completion_wait == STOP_WAIT_SECONDS


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_docker_waits_longer_than_the_worker_does(name: str) -> None:
    services = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))["services"]
    grace = services["worker"].get("stop_grace_period")

    assert grace is not None, f"the worker in {name} has Docker's ten seconds"
    # A margin for arq to close its pool and the container to exit after the wait.
    assert seconds(str(grace)) >= STOP_WAIT_SECONDS + 30, (
        f"{name} kills a stopping worker after {grace}, before it stops waiting for its job "
        f"({STOP_WAIT_SECONDS} s)"
    )


@pytest.mark.parametrize(
    ("duration", "expected"), [("15m", 900), ("1h30m", 5400), ("90s", 90), ("1m30s", 90)]
)
def test_reads_compose_durations(duration: str, expected: int) -> None:
    assert seconds(duration) == expected
