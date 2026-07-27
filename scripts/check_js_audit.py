"""Gate `npm audit` on an allowlist of advisories we have argued about in writing.

`npm audit --audit-level=high` answers one question: *is an installed version vulnerable?* That
is objective, and it is not the question a build should fail on. The question that matters is
*is the vulnerable path reachable from our code?* — and only someone who has read our code can
answer it. When the two answers disagree, a project has three bad options and one good one: pin
to a worse version, lower `--audit-level` and go blind to every future high, live with a red
build until the red means nothing, or **write the argument down and let the build check it**.

This is the fourth. An advisory passes only if `.github/audit-allowlist.json` carries an entry
that names it, explains why it does not reach us, and carries a date by which the argument must
be made again.

Three rules, because "ignore this id" alone is just a mute button:

1. **Unexcepted** — a high/critical advisory with no entry fails. The alarm stays armed for
   everything nobody has looked at.
2. **Expired** — an entry past its `review_by` fails. An exception has a shelf life; the build
   makes you re-decide instead of letting the argument rot.
3. **Stale** — an entry matching nothing in today's report fails. When the upstream fix lands,
   the build tells you to delete the line, so the file never accumulates exceptions that silence
   something nobody remembers.

The report shape matters more than it looks. `npm audit` counted two high "vulnerabilities" for
one advisory: `react-router` carries the advisory object, and `react-router-dom` is listed too
with `via: ["react-router"]` — a *string*, because it is an effect of the vulnerable package,
not a vulnerable package. Walking the top-level map would produce a finding with no advisory id,
which no allowlist could ever name and no build could ever turn green. So we walk the advisory
objects inside `via` and dedupe, which is the thing an entry can actually be written about.

Reads `npm audit --json` on stdin so it is testable without a network or a node_modules.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# The severities that fail a build, matching the `--audit-level=high` this step replaces.
BLOCKING = frozenset({"high", "critical"})

# npm's own version stamp for the report format. Pinning it means a future npm that reshapes the
# JSON fails loudly here instead of quietly finding zero advisories in a structure it no longer
# understands — the worst possible outcome for a security gate is passing for the wrong reason.
REPORT_VERSION = 2

DEFAULT_ALLOWLIST = Path(".github/audit-allowlist.json")


@dataclass(frozen=True, slots=True)
class Advisory:
    """One published advisory against one package in the installed tree."""

    id: str
    package: str
    severity: str
    title: str
    url: str

    @property
    def key(self) -> tuple[str, str]:
        """What an allowlist entry has to name: the advisory *and* the package it lands on."""
        return (self.id, self.package)


@dataclass(frozen=True, slots=True)
class Allowed:
    """A written, dated argument that one advisory does not reach this codebase."""

    id: str
    package: str
    reason: str
    decided_by: str
    review_by: date

    @property
    def key(self) -> tuple[str, str]:
        return (self.id, self.package)


class AuditError(Exception):
    """The gate could not run — a malformed report or a malformed allowlist."""


def advisory_id(via: dict[str, Any]) -> str:
    """The public identifier of an advisory, preferring the GHSA in its URL.

    npm's own numeric `source` is stable but namespaced to npm; the GHSA is what the advisory is
    called everywhere else, including in the entry a human writes. Fall back to the number when
    there is no GHSA so that every finding stays nameable — an advisory nobody can name is an
    advisory nobody can except, and the build would be stuck red with no way out.
    """
    url = str(via.get("url", ""))
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.startswith("GHSA-"):
        return tail
    return f"npm:{via.get('source', 'unknown')}"


def parse_report(raw: str) -> list[Advisory]:
    """Every blocking advisory in an `npm audit --json` report, deduped and sorted."""
    try:
        # Windows PowerShell writes UTF-8 *with* a BOM, so a developer reproducing the CI step
        # locally would otherwise be told "npm audit did not produce JSON" about a perfectly good
        # report. The CI runner is bash and never hits this; the person debugging is the one who
        # does, and that is exactly when a misleading error costs the most.
        report = json.loads(raw.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        raise AuditError(f"npm audit did not produce JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise AuditError("npm audit report is not an object")

    version = report.get("auditReportVersion")
    if version != REPORT_VERSION:
        raise AuditError(
            f"unknown npm audit report version {version!r} (expected {REPORT_VERSION}); "
            "the parser below was written against that shape and must be re-read before trusting"
        )
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise AuditError("npm audit report has no `vulnerabilities` object")

    found: dict[tuple[str, str], Advisory] = {}
    for name, entry in vulnerabilities.items():
        # Skipping a malformed entry would be the exact failure this file exists to prevent:
        # a gate that reports "clean" about something it could not read.
        if not isinstance(entry, dict):
            raise AuditError(f"vulnerability entry for {name!r} is not an object")
        for via in entry.get("via", []):
            # A string here names a package that is vulnerable *because* of something else. The
            # advisory itself is an object, and it appears on the package it actually hits.
            if not isinstance(via, dict):
                continue
            if str(via.get("severity", "")).lower() not in BLOCKING:
                continue
            advisory = Advisory(
                id=advisory_id(via),
                package=str(via.get("name", "")),
                severity=str(via.get("severity", "")).lower(),
                title=str(via.get("title", "")),
                url=str(via.get("url", "")),
            )
            found.setdefault(advisory.key, advisory)
    return sorted(found.values(), key=lambda a: a.key)


def parse_allowlist(raw: str) -> list[Allowed]:
    """The written exceptions, refusing anything malformed.

    A typo in a key would otherwise produce an entry that silently matches nothing — which reads
    as "we decided about this" while behaving as "we did not". Fail instead.
    """
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditError(f"allowlist is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("allow"), list):
        raise AuditError("allowlist must be an object with an `allow` array")

    required = ("id", "package", "reason", "decided_by", "review_by")
    entries: list[Allowed] = []
    for index, item in enumerate(document["allow"]):
        where = f"allow[{index}]"
        if not isinstance(item, dict):
            raise AuditError(f"{where} is not an object")
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise AuditError(f"{where} is missing {', '.join(missing)}")
        unknown = sorted(set(item) - set(required))
        if unknown:
            raise AuditError(f"{where} has unknown keys: {', '.join(unknown)}")
        try:
            review_by = date.fromisoformat(str(item["review_by"]))
        except ValueError as exc:
            raise AuditError(f"{where} has an invalid review_by: {exc}") from exc
        entries.append(
            Allowed(
                id=str(item["id"]),
                package=str(item["package"]),
                reason=str(item["reason"]),
                decided_by=str(item["decided_by"]),
                review_by=review_by,
            )
        )
    duplicates = {e.key for e in entries if sum(o.key == e.key for o in entries) > 1}
    if duplicates:
        listed = ", ".join(f"{i} on {p}" for i, p in sorted(duplicates))
        raise AuditError(f"allowlist has duplicate entries: {listed}")
    return entries


def check(advisories: Sequence[Advisory], allowed: Sequence[Allowed], today: date) -> list[str]:
    """Every reason this build should fail, in the order a reader wants them."""
    index = {entry.key: entry for entry in allowed}
    matched: set[tuple[str, str]] = set()
    problems: list[str] = []

    for advisory in advisories:
        entry = index.get(advisory.key)
        if entry is None:
            problems.append(
                f"UNEXCEPTED  {advisory.severity:8} {advisory.id} on {advisory.package}\n"
                f"            {advisory.title}\n"
                f"            {advisory.url}\n"
                "            Fix it, or add a dated entry to the allowlist saying why the "
                "vulnerable path cannot be reached from this codebase."
            )
            continue
        matched.add(advisory.key)
        if entry.review_by < today:
            problems.append(
                f"EXPIRED     {entry.id} on {entry.package} was due for review on "
                f"{entry.review_by.isoformat()} (today is {today.isoformat()}).\n"
                f"            Reason on file: {entry.reason}\n"
                "            Re-read the advisory and either fix it or move review_by forward."
            )

    for key, entry in sorted(index.items()):
        if key not in matched:
            problems.append(
                f"STALE       {entry.id} on {entry.package} matches nothing in this report — "
                "the advisory is gone.\n"
                "            Delete the entry: an exception nobody can point at is one that "
                "silences something nobody remembers."
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help=f"the written exceptions (default: {DEFAULT_ALLOWLIST})",
    )
    args = parser.parse_args(argv)

    try:
        advisories = parse_report(sys.stdin.read())
        allowed = parse_allowlist(args.allowlist.read_text(encoding="utf-8"))
    except (AuditError, OSError) as exc:
        print(f"npm audit gate could not run: {exc}", file=sys.stderr)
        return 2

    problems = check(advisories, allowed, datetime.now(UTC).date())
    excepted = len(advisories) - sum(p.startswith("UNEXCEPTED") for p in problems)
    if problems:
        print(f"npm audit gate failed with {len(problems)} problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}\n", file=sys.stderr)
        return 1

    print(
        f"npm audit clean: {len(advisories)} blocking advisory(ies), "
        f"{excepted} covered by a dated exception."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
