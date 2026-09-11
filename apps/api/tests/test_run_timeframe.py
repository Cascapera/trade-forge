"""The run's timeframe against the document's — `runner.timeframe_refusal`.

A document carries a `timeframe` and a run carries another, and until this function existed
nothing compared them. The engine steps the loop at the run's while the setup builds its
higher-timeframe bars out of the document's, so the two disagreeing is a wrong backtest with
nothing in it that looks wrong.

These cases are organised around the one distinction that matters: **the document's timeframe
reaches the engine only when the document carries an `htf`.** A guard that refused every
disagreement would pass half of them and break re-running a filterless strategy anywhere else —
which is a feature that has worked since the launch screen existed.
"""

from typing import Any

import pytest

from tradeforge_api.runner import timeframe_refusal


def filtered(timeframe: str, htf: str = "H4") -> dict[str, Any]:
    """His structure entry under a higher-timeframe filter — the shipped fixture's shape."""
    return {
        "schema_version": "1.0",
        "name": f"CHoCH {timeframe} under {htf}",
        "timeframe": timeframe,
        "setup": {"type": "structure_choch", "params": {"htf": htf, "htf_offset": 3}},
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 3}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 0.5}}},
    }


def unfiltered(timeframe: str) -> dict[str, Any]:
    """The same entry with no filter — so the document's timeframe reaches nothing."""
    document = filtered(timeframe)
    document["name"] = f"CHoCH {timeframe}"
    document["setup"] = {"type": "structure_choch", "params": {}}
    return document


class TestTheFilterlessDocumentIsNeverRefused:
    """⚠️ The half a "refuse any disagreement" guard would get wrong.

    Without `htf`, `setup_factory` does not pass the document's timeframe to the setup at all,
    and `CompiledStrategy.timeframe` is written once and read nowhere. So these runs are exactly
    as safe as they have always been, and refusing them would break `Re-run saved`.
    """

    @pytest.mark.parametrize("run_at", ["M5", "M15", "M30", "H1", "H4", "D1"])
    def test_runs_at_any_timeframe(self, run_at: str) -> None:
        assert timeframe_refusal(unfiltered("M15"), run_at) is None

    def test_including_the_one_that_would_be_refused_with_a_filter(self) -> None:
        # H4 against a document saved at M15: refused below when a filter is present, allowed
        # here. Same two timeframes, opposite answers — which is what proves the guard is
        # reading the filter and not merely comparing two strings.
        assert timeframe_refusal(unfiltered("M15"), "H4") is None
        assert timeframe_refusal(filtered("M15"), "H4") is not None


class TestTheFilteredDocument:
    def test_at_its_own_timeframe_it_runs(self) -> None:
        assert timeframe_refusal(filtered("M15"), "M15") is None

    @pytest.mark.parametrize("run_at", ["M5", "M15", "M30", "H1"])
    def test_runs_wherever_the_filter_is_still_coarser(self, run_at: str) -> None:
        # H4 is a whole number of each of these, so the filter still means something.
        assert timeframe_refusal(filtered("M15"), run_at) is None

    def test_is_refused_when_the_run_reaches_the_filter(self) -> None:
        # ⚠️ The silent case, and the reason this module exists. The engine raises nothing here:
        # it assembles one "H4" bar per H4 bar, a bar late, and the filter becomes a lagged copy
        # of the chart. Measured 2026-09-11: six bars in, five out.
        reason = timeframe_refusal(filtered("M15"), "H4")
        assert reason is not None
        assert "htf" in reason or "coarser" in reason

    def test_is_refused_when_the_run_is_coarser_than_the_filter(self) -> None:
        # D1 under an H4 filter: the filter is now *finer* than the chart and has nothing to
        # build from. The engine's own failure for this one is loud, but a 422 beats a run that
        # reaches `failed` after being queued.
        assert timeframe_refusal(filtered("M15"), "D1") is not None

    def test_names_the_field_a_client_can_point_at(self) -> None:
        # The message is `semantic.py`'s own, not a sentence written here — so a screen that
        # already renders a semantic refusal renders this one unchanged, and the day the rule
        # changes there is one place to change.
        reason = timeframe_refusal(filtered("M15"), "H4")
        assert reason is not None
        assert "setup.params.htf" in reason


class TestTheDocumentItself:
    def test_the_question_is_about_the_run_not_about_the_stored_row(self) -> None:
        # ⚠️ A document stored at H4 under an H4 filter is not runnable as written, and
        # `assert_executable` would have refused to save it. Run at M15 it is **allowed** — the
        # substitution makes the filter coarser than the chart, which is the whole rule.
        #
        # This is the function's contract said out loud: it answers "can this document run at
        # this timeframe", never "does the stored timeframe look right". A guard that compared
        # the stored field against the run would answer the opposite on both lines below.
        document = filtered("H4", htf="H4")
        assert timeframe_refusal(document, "M15") is None
        assert timeframe_refusal(document, "H4") is not None

    def test_a_corrupt_document_is_reported_as_a_different_fact(self) -> None:
        # ⚠️ Not dressed up as a timeframe problem. A document that no longer validates is a
        # different failure with a different fix, and folding the two would send a reader to
        # change their timeframe over a field that is missing.
        broken = filtered("M15")
        del broken["risk"]
        reason = timeframe_refusal(broken, "M15")
        assert reason is not None
        assert "no longer validates" in reason
