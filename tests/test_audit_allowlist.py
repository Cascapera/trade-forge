"""Tests for the npm audit gate (ADR-0017).

This is the mechanism that decides when a published advisory is allowed to *not* fail the build.
A bug here is invisible in the worst way: the gate keeps printing "clean" while it has stopped
looking. So the tests below are written to fail if the gate ever silences more than the one
advisory somebody wrote an argument for — a different advisory on the same package, the same
advisory on a different package, an entry past its date, and an entry that no longer matches
anything all have their own test.
"""

import io
import json
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import check_js_audit as gate

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_ALLOWLIST = REPO_ROOT / ".github" / "audit-allowlist.json"

REACT_ROUTER = "GHSA-qwww-vcr4-c8h2"
TODAY = date(2026, 7, 27)


def _report(vulnerabilities: dict[str, Any], version: int = gate.REPORT_VERSION) -> str:
    return json.dumps({"auditReportVersion": version, "vulnerabilities": vulnerabilities})


def _via(
    *,
    ghsa: str = REACT_ROUTER,
    package: str = "react-router",
    severity: str = "high",
    source: int = 1124282,
) -> dict[str, Any]:
    return {
        "source": source,
        "name": package,
        "dependency": package,
        "title": f"advisory {ghsa}",
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": severity,
        "cwe": "CWE-352",
        "range": ">=7.12.0 <8.3.0",
    }


def _entry(
    *,
    ghsa: str = REACT_ROUTER,
    package: str = "react-router",
    review_by: str = "2026-10-27",
) -> dict[str, str]:
    return {
        "id": ghsa,
        "package": package,
        "reason": "not reachable: the app has no server",
        "decided_by": "Guilherme Farias Cascapera",
        "review_by": review_by,
    }


def _allowlist(*entries: dict[str, str]) -> str:
    return json.dumps({"allow": list(entries)})


# --------------------------------------------------------------------------- #
# Reading the report                                                            #
# --------------------------------------------------------------------------- #


def test_a_clean_tree_has_no_advisories() -> None:
    assert gate.parse_report(_report({})) == []


def test_the_real_report_shape_yields_one_advisory_not_two() -> None:
    """The golden, captured from `npm audit --json` on this repo (27/07/2026).

    npm counts two high *vulnerabilities* here, but there is one *advisory*: `react-router`
    carries the advisory object, and `react-router-dom` is listed with `via: ["react-router"]`
    — a string, because it is an effect of the vulnerable package rather than a vulnerable
    package itself. Walking the top-level map instead of the `via` objects would invent a second
    finding carrying no advisory id, which no allowlist entry could ever name and no build could
    ever turn green. This test is what stops that modelling mistake from coming back.
    """
    advisories = gate.parse_report(
        _report(
            {
                "react-router": {
                    "name": "react-router",
                    "severity": "high",
                    "isDirect": False,
                    "via": [_via()],
                    "effects": ["react-router-dom"],
                    "range": "7.12.0 - 8.2.0",
                    "nodes": ["node_modules/react-router"],
                },
                "react-router-dom": {
                    "name": "react-router-dom",
                    "severity": "high",
                    "isDirect": True,
                    "via": ["react-router"],
                    "effects": [],
                    "range": ">=7.12.0-pre.0",
                    "nodes": ["node_modules/react-router-dom"],
                },
            }
        )
    )

    assert [(a.id, a.package, a.severity) for a in advisories] == [
        (REACT_ROUTER, "react-router", "high")
    ]


def test_the_same_advisory_reported_twice_is_one_finding() -> None:
    advisories = gate.parse_report(
        _report(
            {
                "react-router": {"via": [_via()]},
                "react-router-again": {"via": [_via()]},
            }
        )
    )

    assert len(advisories) == 1


@pytest.mark.parametrize("severity", ["info", "low", "moderate"])
def test_advisories_below_high_do_not_block(severity: str) -> None:
    assert gate.parse_report(_report({"pkg": {"via": [_via(severity=severity)]}})) == []


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_high_and_critical_block(severity: str) -> None:
    advisories = gate.parse_report(_report({"pkg": {"via": [_via(severity=severity)]}}))

    assert [a.severity for a in advisories] == [severity]


def test_an_advisory_without_a_ghsa_is_still_nameable() -> None:
    """Otherwise the build would be stuck red with no entry that could ever cover it."""
    via = _via() | {"url": "https://example.invalid/whatever"}
    advisories = gate.parse_report(_report({"pkg": {"via": [via]}}))

    assert [a.id for a in advisories] == ["npm:1124282"]


def test_a_report_written_by_powershell_still_parses() -> None:
    """Windows PowerShell writes UTF-8 with a BOM; the report is fine, the first byte is not."""
    advisories = gate.parse_report("﻿" + _report({"react-router": {"via": [_via()]}}))

    assert [a.id for a in advisories] == [REACT_ROUTER]


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json at all", "did not produce JSON"),
        ("[1, 2, 3]", "not an object"),
        (json.dumps({"auditReportVersion": 3, "vulnerabilities": {}}), "unknown npm audit report"),
        (json.dumps({"vulnerabilities": {}}), "unknown npm audit report"),
        (json.dumps({"auditReportVersion": 2}), "no `vulnerabilities` object"),
        (_report({"react-router": "surprise"}), "'react-router' is not an object"),
    ],
)
def test_an_unreadable_report_fails_loudly(raw: str, fragment: str) -> None:
    """A security gate that cannot read its input must never report "clean"."""
    with pytest.raises(gate.AuditError, match=fragment):
        gate.parse_report(raw)


# --------------------------------------------------------------------------- #
# Reading the allowlist                                                         #
# --------------------------------------------------------------------------- #


def test_a_well_formed_entry_parses() -> None:
    (allowed,) = gate.parse_allowlist(_allowlist(_entry()))

    assert allowed.key == (REACT_ROUTER, "react-router")
    assert allowed.review_by == date(2026, 10, 27)


@pytest.mark.parametrize("field", ["id", "package", "reason", "decided_by", "review_by"])
def test_an_entry_missing_any_field_is_refused(field: str) -> None:
    """Every field is load-bearing: an entry without a reason or a date is a mute button."""
    entry = _entry()
    del entry[field]

    with pytest.raises(gate.AuditError, match=f"missing {field}"):
        gate.parse_allowlist(_allowlist(entry))


@pytest.mark.parametrize("field", ["id", "package", "reason", "decided_by", "review_by"])
def test_an_entry_with_a_blank_field_is_refused(field: str) -> None:
    with pytest.raises(gate.AuditError, match=f"missing {field}"):
        gate.parse_allowlist(_allowlist(_entry() | {field: "   "}))


def test_an_entry_with_an_unknown_key_is_refused() -> None:
    """A misspelled key would otherwise read as decided and behave as undecided."""
    with pytest.raises(gate.AuditError, match="unknown keys: reviewed_by"):
        gate.parse_allowlist(_allowlist(_entry() | {"reviewed_by": "2026-10-27"}))


def test_an_entry_with_an_unparseable_date_is_refused() -> None:
    with pytest.raises(gate.AuditError, match="invalid review_by"):
        gate.parse_allowlist(_allowlist(_entry(review_by="soon")))


def test_duplicate_entries_are_refused() -> None:
    with pytest.raises(gate.AuditError, match="duplicate entries"):
        gate.parse_allowlist(_allowlist(_entry(), _entry()))


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json", "not valid JSON"),
        (json.dumps([]), "`allow` array"),
        (json.dumps({"allow": {}}), "`allow` array"),
        (json.dumps({"allow": ["nope"]}), "not an object"),
    ],
)
def test_a_malformed_allowlist_is_refused(raw: str, fragment: str) -> None:
    with pytest.raises(gate.AuditError, match=fragment):
        gate.parse_allowlist(raw)


# --------------------------------------------------------------------------- #
# The three rules                                                               #
# --------------------------------------------------------------------------- #


def test_an_advisory_with_no_entry_fails_the_build() -> None:
    advisories = gate.parse_report(_report({"react-router": {"via": [_via()]}}))

    (problem,) = gate.check(advisories, [], TODAY)

    assert problem.startswith("UNEXCEPTED")
    assert REACT_ROUTER in problem


def test_the_written_exception_lets_the_build_pass() -> None:
    advisories = gate.parse_report(_report({"react-router": {"via": [_via()]}}))
    allowed = gate.parse_allowlist(_allowlist(_entry()))

    assert gate.check(advisories, allowed, TODAY) == []


def test_a_different_advisory_on_the_same_package_still_fails() -> None:
    """The exception covers one advisory, not one package — this is the whole point of the gate.

    Without it, "we looked at react-router once" would silence every future advisory against
    react-router, including one that *does* reach us. Two problems come out, and both are the
    point: the new advisory is unexcepted, and the old entry has stopped matching anything.
    """
    advisories = gate.parse_report(
        _report({"react-router": {"via": [_via(ghsa="GHSA-aaaa-bbbb-cccc")]}})
    )
    allowed = gate.parse_allowlist(_allowlist(_entry()))

    unexcepted, stale = gate.check(advisories, allowed, TODAY)

    assert unexcepted.startswith("UNEXCEPTED")
    assert "GHSA-aaaa-bbbb-cccc" in unexcepted
    assert stale.startswith("STALE")
    assert REACT_ROUTER in stale


def test_the_same_advisory_on_a_different_package_still_fails() -> None:
    """The entry names a pair. A monorepo advisory that spreads to a second package is new work."""
    advisories = gate.parse_report(
        _report({"other": {"via": [_via(package="react-router-native")]}})
    )
    allowed = gate.parse_allowlist(_allowlist(_entry()))

    unexcepted, stale = gate.check(advisories, allowed, TODAY)

    assert unexcepted.startswith("UNEXCEPTED")
    assert "react-router-native" in unexcepted
    assert stale.startswith("STALE")


def test_an_expired_exception_fails_the_build() -> None:
    """An exception has a shelf life: the build makes you re-decide instead of letting it rot."""
    advisories = gate.parse_report(_report({"react-router": {"via": [_via()]}}))
    allowed = gate.parse_allowlist(_allowlist(_entry(review_by="2026-07-26")))

    (problem,) = gate.check(advisories, allowed, TODAY)

    assert problem.startswith("EXPIRED")
    assert "2026-07-26" in problem


def test_an_exception_reviewed_today_still_passes() -> None:
    """The boundary is `<`, not `<=`: the day it is due is the last day it is good."""
    advisories = gate.parse_report(_report({"react-router": {"via": [_via()]}}))
    allowed = gate.parse_allowlist(_allowlist(_entry(review_by=TODAY.isoformat())))

    assert gate.check(advisories, allowed, TODAY) == []


def test_an_exception_that_matches_nothing_fails_the_build() -> None:
    """When the upstream fix lands, the build asks you to delete the line.

    An exception nobody can point at is one that silences something nobody remembers.
    """
    allowed = gate.parse_allowlist(_allowlist(_entry()))

    (problem,) = gate.check([], allowed, TODAY)

    assert problem.startswith("STALE")
    assert REACT_ROUTER in problem


def test_an_expired_exception_is_not_also_reported_as_stale() -> None:
    advisories = gate.parse_report(_report({"react-router": {"via": [_via()]}}))
    allowed = gate.parse_allowlist(_allowlist(_entry(review_by="2020-01-01")))

    problems = gate.check(advisories, allowed, TODAY)

    assert len(problems) == 1


def test_every_unexcepted_advisory_is_reported_not_just_the_first() -> None:
    advisories = gate.parse_report(
        _report(
            {
                "a": {"via": [_via(ghsa="GHSA-1111-1111-1111", package="a")]},
                "b": {"via": [_via(ghsa="GHSA-2222-2222-2222", package="b")]},
            }
        )
    )

    problems = gate.check(advisories, [], TODAY)

    assert len(problems) == 2


# --------------------------------------------------------------------------- #
# The command line, and the file this repo actually ships                       #
# --------------------------------------------------------------------------- #


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: str,
    allowlist: str,
) -> int:
    path = tmp_path / "allowlist.json"
    path.write_text(allowlist, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(report))
    return gate.main(["--allowlist", str(path)])


def test_main_passes_when_every_advisory_is_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        monkeypatch,
        tmp_path,
        _report({"react-router": {"via": [_via()]}}),
        _allowlist(_entry(review_by="2099-01-01")),
    )

    assert code == 0
    assert "1 covered by a dated exception" in capsys.readouterr().out


def test_main_fails_on_an_uncovered_advisory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code = _run(monkeypatch, tmp_path, _report({"react-router": {"via": [_via()]}}), _allowlist())

    assert code == 1


def test_main_separates_could_not_run_from_found_a_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit 2 means the gate never looked; exit 1 means it looked and did not like what it saw."""
    code = _run(monkeypatch, tmp_path, "npm exploded", _allowlist())

    assert code == 2


def test_main_fails_when_the_allowlist_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(_report({})))

    assert gate.main(["--allowlist", str(tmp_path / "nope.json")]) == 2


def test_the_allowlist_this_repo_ships_is_valid() -> None:
    """A typo in the committed file would only ever show up as a red CI run. Catch it here.

    ⚠️ The list is **empty today, and empty is the good state** — every advisory in the report
    has to be zero. It is not the same as the file being absent: the gate exits 2 on a missing
    allowlist, because a gate that cannot find its own configuration must not report clean.

    It held `GHSA-qwww-vcr4-c8h2` (react-router CSRF in RSC Mode) from 27/07 to 24/08/2026.
    `react-router` 7.18.2 fixed it on the 7.x line, the entry stopped matching anything, and the
    gate failed **on the stale entry** — which is the behaviour ADR-0017 designed and the reason
    this file no longer silences anything.

    The assertions are written to hold either way, so the day somebody adds a new exception this
    test still says what an exception has to be, rather than needing to be rewritten first.
    """
    allowed = gate.parse_allowlist(REAL_ALLOWLIST.read_text(encoding="utf-8"))

    assert allowed == [], (
        "an advisory is being silenced; that is allowed, but the entry must be reviewed here: "
        f"{[entry.key for entry in allowed]}"
    )
    assert all(len(entry.reason) > 80 for entry in allowed), "an entry must argue, not assert"
    assert all(entry.review_by is not None for entry in allowed), "an entry must expire"

    # ⚠️ Both assertions above are vacuous while the list is empty — `all([])` is True, so a
    # renamed field would sail through them and only fail the day somebody adds an exception.
    # This is what keeps them honest: the names they reach for have to exist right now.
    assert {field.name for field in fields(gate.Allowed)} >= {"reason", "review_by"}
