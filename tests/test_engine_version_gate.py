"""Tests for the engine-version gate (28/09).

The gate is what keeps `tradeforge_api.reuse` honest: a run is copied instead of run only under
the same engine version. A bug here is silent — the gate passes, and a later sweep receives the
results of an engine that no longer exists. So each way through it has its own test, and so does
each way it must not let a change through.
"""

from pathlib import Path

import check_engine_version as gate

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_FILE = "packages/engine/src/tradeforge_engine/strategies/structure.py"


class TestVerdict:
    def test_a_change_outside_the_engine_passes(self) -> None:
        result = gate.verdict(["apps/api/src/tradeforge_api/reuse.py"], "0.2.0", "0.2.0", [])

        assert result.passed

    def test_the_engine_changed_without_its_version_fails_naming_the_files(self) -> None:
        result = gate.verdict([ENGINE_FILE, "apps/web/x.ts"], "0.2.0", "0.2.0", ["fix: a gift"])

        assert not result.passed
        assert ENGINE_FILE in result.reason
        assert "apps/web/x.ts" not in result.reason
        assert "Engine-Results: unchanged" in result.reason

    def test_the_engine_changed_with_its_version_raised_passes(self) -> None:
        result = gate.verdict([ENGINE_FILE], "0.2.0", "0.3.0", [])

        assert result.passed
        assert "0.2.0 -> 0.3.0" in result.reason

    def test_declared_unchanged_with_a_reason_passes(self) -> None:
        message = (
            "refactor: a pure tracker\n\nEngine-Results: unchanged — batch equivalence tests\n"
        )

        result = gate.verdict([ENGINE_FILE], "0.2.0", "0.2.0", ["other", message])

        assert result.passed
        assert "batch equivalence tests" in result.reason

    def test_declared_unchanged_without_a_reason_fails(self) -> None:
        """The line alone would be a mute button; with a reason it is a claim to check."""
        for bare in ("Engine-Results: unchanged", "Engine-Results: unchanged —  "):
            result = gate.verdict([ENGINE_FILE], "0.2.0", "0.2.0", [f"refactor: x\n\n{bare}\n"])

            assert not result.passed, bare

    def test_the_line_counts_only_at_the_start_of_a_line(self) -> None:
        quoted = "docs: explain the gate\n\nwrite `Engine-Results: unchanged — x` when proven\n"

        result = gate.verdict([ENGINE_FILE], "0.2.0", "0.2.0", [quoted])

        assert not result.passed

    def test_an_engine_that_declares_no_version_fails(self) -> None:
        result = gate.verdict([ENGINE_FILE], "0.2.0", None, [])

        assert not result.passed

    def test_the_engine_tests_alone_are_not_the_engine(self) -> None:
        result = gate.verdict(["packages/engine/tests/test_swing.py"], "0.2.0", "0.2.0", [])

        assert result.passed


class TestVersionIn:
    def test_reads_the_real_engine_version(self) -> None:
        source = (REPO_ROOT / gate.VERSION_FILE).read_text(encoding="utf-8")

        assert gate.version_in(source) == "0.6.0"

    def test_none_when_there_is_none(self) -> None:
        assert gate.version_in('__all__ = ["x"]\n') is None


def test_the_command_refuses_a_wrong_number_of_arguments() -> None:
    assert gate.main(["only-one"]) == 2
