"""Fail a pull request that changes the engine without saying what happened to its results.

A finished run is reused for a new one only under the same engine version
(`tradeforge_api.reuse`, 28/09): the engine is deterministic, so the same inputs under the same
engine give the same answer. That holds only if the version moves whenever an answer can. It sat
at 0.1.0 from July to 28/09 through many fixes that changed trades — and nothing noticed, because
nothing asked.

This asks. A pull request that touches `packages/engine/src` passes in one of two ways:

1. **It raises `tradeforge_engine.__version__`** — results may change, and every run made before
   stops being an original for reuse.
2. **One of its commits says `Engine-Results: unchanged — <why>`** — a refactor or a speed-up
   proven identical (the batch equivalence tests, a golden test), where raising the version would
   throw away every reusable run for nothing. The line is the claim, written where it stays; the
   engine-guardian checks it.

Anything else fails, naming the files and the two ways out.

Usage (CI): `python scripts/check_engine_version.py <base-sha> <head-sha>`.
"""

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

ENGINE_SOURCE = "packages/engine/src/"
VERSION_FILE = "packages/engine/src/tradeforge_engine/__init__.py"

_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
# The separator after `unchanged` may be an em dash, an en dash, a colon or a hyphen — the dashes by
# names, so the source holds no character a reader could mistake for a hyphen.
_DASHES = "\N{EM DASH}\N{EN DASH}"
_UNCHANGED = re.compile(
    rf"^Engine-Results:\s*unchanged\b\s*[{_DASHES}:-]?\s*(\S.*)?$", re.MULTILINE
)


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reason: str


def version_in(source: str) -> str | None:
    """The `__version__` a module's text declares, or `None` if it declares none."""
    found = _VERSION.search(source)
    return None if found is None else found.group(1)


def declared_unchanged(messages: Sequence[str]) -> str | None:
    """The reason of the first `Engine-Results: unchanged — <why>` line, or `None`.

    ⚠️ **A reason is required.** The line alone would become the mute button every hurried commit
    reaches for; with a reason it is a claim somebody can check.
    """
    for message in messages:
        for found in _UNCHANGED.finditer(message):
            reason = (found.group(1) or "").strip()
            if reason:
                return reason
    return None


def verdict(
    changed: Sequence[str],
    base_version: str | None,
    head_version: str | None,
    messages: Sequence[str],
) -> Verdict:
    """Whether a pull request with these changed files, versions and commit messages may pass."""
    touched = sorted(path for path in changed if path.startswith(ENGINE_SOURCE))
    if not touched:
        return Verdict(passed=True, reason="the engine's source is untouched")
    if head_version is None:
        return Verdict(passed=False, reason=f"{VERSION_FILE} declares no __version__")
    if base_version != head_version:
        return Verdict(
            passed=True, reason=f"the engine's version moved: {base_version} -> {head_version}"
        )
    unchanged = declared_unchanged(messages)
    if unchanged is not None:
        return Verdict(passed=True, reason=f"declared unchanged: {unchanged}")
    listed = "\n".join(f"  {path}" for path in touched)
    return Verdict(
        passed=False,
        reason=(
            f"the engine changed and its version did not ({head_version}):\n{listed}\n\n"
            "A finished run is reused only under the same version (tradeforge_api.reuse), so a "
            "change that can move a result must raise tradeforge_engine.__version__ (and "
            "packages/engine/pyproject.toml). A change proven not to move any result says so in "
            "a commit of this PR, with the proof:\n"
            "  Engine-Results: unchanged — <why, e.g. the batch equivalence tests pass>"
        ),
    )


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed program, arguments from this script
        ["git", *args],  # noqa: S607 — `git` from the runner's PATH, as every step uses it
        check=True,
        capture_output=True,
        text=True,
        # Commit messages carry em dashes; the runner's locale is not a promise.
        encoding="utf-8",
    ).stdout


def _version_at(sha: str) -> str | None:
    try:
        return version_in(_git("show", f"{sha}:{VERSION_FILE}"))
    except subprocess.CalledProcessError:
        return None


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:  # noqa: PLR2004 — base and head
        print("usage: check_engine_version.py <base-sha> <head-sha>", file=sys.stderr)
        return 2
    base, head = argv
    changed = _git("diff", "--name-only", f"{base}...{head}").split()
    messages = _git("log", "--format=%B%x00", f"{base}..{head}").split("\x00")
    result = verdict(changed, _version_at(base), _version_at(head), messages)
    print(("ok: " if result.passed else "FAIL: ") + result.reason)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
