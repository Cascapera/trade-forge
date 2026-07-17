"""`tradeforge-db`, dispatched against fakes.

What is worth testing here is the dispatch and the guard rails — that `downgrade`
refuses to run without being told where to, that `upgrade` defaults to head. Whether
Alembic can talk to Postgres is the integration suite's job.
"""

import pytest

from tradeforge_db import cli


def test_upgrade_defaults_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(cli, "upgrade", applied.append)

    assert cli.main(["upgrade"]) == 0
    assert applied == ["head"]


def test_upgrade_accepts_an_explicit_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(cli, "upgrade", applied.append)

    assert cli.main(["upgrade", "0001"]) == 0
    assert applied == ["0001"]


def test_downgrade_refuses_to_guess(capsys: pytest.CaptureFixture[str]) -> None:
    """No default revision: `downgrade` with a stray enter would drop tables.

    argparse exits 2 on a missing argument, which is the right answer — the command
    never reaches Alembic.
    """
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["downgrade"])

    assert exit_info.value.code == 2
    assert "revision" in capsys.readouterr().err


def test_downgrade_passes_the_revision_through(monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(cli, "downgrade", applied.append)

    assert cli.main(["downgrade", "base"]) == 0
    assert applied == ["base"]


def test_an_unknown_command_is_an_error() -> None:
    with pytest.raises(SystemExit):
        cli.main(["migrate-everything"])
