"""The CLI's contract is its exit code: green means every dependency answered."""

import sys

import pytest

from tradeforge_api import cli
from tradeforge_api.health import ServiceHealth


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")


def test_exits_zero_when_every_service_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "check_postgres", lambda _: ServiceHealth("postgres", True, "up"))
    monkeypatch.setattr(cli, "check_redis", lambda _: ServiceHealth("redis", True, "up"))

    assert cli.main() == 0
    assert "[OK  ] postgres" in capsys.readouterr().out


def test_exits_non_zero_when_one_service_is_down(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One bad service fails the whole check — a partially-up stack is down."""
    monkeypatch.setattr(cli, "check_postgres", lambda _: ServiceHealth("postgres", True, "up"))
    monkeypatch.setattr(cli, "check_redis", lambda _: ServiceHealth("redis", False, "refused"))

    assert cli.main() == 1
    assert "[FAIL] redis" in capsys.readouterr().out


class Cp1252Console:
    """A Windows console: it encodes on write, and raises on what it cannot render."""

    encoding = "cp1252"

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> int:
        text.encode(self.encoding)  # raises UnicodeEncodeError, exactly as the real one does
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass


def test_reports_a_failure_a_windows_console_cannot_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the tool must survive delivering the very message it exists to deliver.

    A Portuguese Windows Postgres answers "autenticação do tipo senha falhou", whose
    bytes psycopg decodes into U+FFFD replacement characters — which cp1252 cannot
    encode back. `print` then raised UnicodeEncodeError and the health check died
    while reporting the outage, i.e. at the only moment it was useful.
    """
    broken = ServiceHealth("postgres", False, "OperationalError: autentica��o falhou")
    console = Cp1252Console()
    monkeypatch.setattr(cli, "check_postgres", lambda _: broken)
    monkeypatch.setattr(cli, "check_redis", lambda _: ServiceHealth("redis", True, "up"))
    monkeypatch.setattr(sys, "stdout", console)

    assert cli.main() == 1
    assert any("[FAIL] postgres" in line for line in console.written)


def test_printable_replaces_what_the_console_cannot_render() -> None:
    assert cli._printable("autenticação", "ascii") == "autentica??o"
