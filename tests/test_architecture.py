"""Architecture invariants, enforced as tests.

A rule that lives only in a document is a rule that gets broken on a Friday. The
invariants below are the load-bearing ones from AGENTS.md §5 — the ones whose
violation would quietly dissolve the design — so they are asserted on every run.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The MT5 library may only be imported at the broker edges (ADR-02).
MT5_ALLOWED_ROOTS = ("apps/collector", "apps/executor")

MT5_IMPORT = re.compile(r"^\s*(?:import\s+MetaTrader5|from\s+MetaTrader5\b)", re.MULTILINE)
APP_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+tradeforge_(api|collector|executor)\b", re.MULTILINE
)

# The engine is pure. Persistence is somebody else's problem (ADR-0009).
DB_IMPORT = re.compile(r"^\s*(?:import|from)\s+tradeforge_db\b", re.MULTILINE)

# The swing family and the structure machine are separate setups, by the author's own
# words. The modules that may not know each other, and the one module that legitimately
# knows both because it is the seam the DSL names.
SWING_MODULES = (
    "packages/engine/src/tradeforge_engine/swing.py",
    "packages/engine/src/tradeforge_engine/average_setups.py",
)
STRUCTURE_IMPORT = re.compile(r"^\s*(?:import|from)\s+tradeforge_engine\.setups\b", re.MULTILINE)


def _source_files(*roots: str) -> list[Path]:
    """Every Python source file under the given repo-relative roots."""
    return [
        path
        for root in roots
        for path in (REPO_ROOT / root).rglob("*.py")
        if ".venv" not in path.parts
    ]


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


@pytest.mark.parametrize(
    "source",
    _source_files("apps", "packages"),
    ids=_relative,
)
def test_metatrader5_stays_behind_the_broker_edge(source: Path) -> None:
    """Only the collector and the executor may know MetaTrader 5 exists.

    This is what keeps ~90% of the system portable to Linux/Docker and makes the
    future swap to a broker API (Alpaca, IBKR) an additive change rather than a
    rewrite. Linux CI enforces the other half: no MT5 wheel exists there at all.
    """
    relative = _relative(source)
    if relative.startswith(MT5_ALLOWED_ROOTS):
        return

    assert not MT5_IMPORT.search(source.read_text(encoding="utf-8")), (
        f"{relative} imports MetaTrader5, but only {' and '.join(MT5_ALLOWED_ROOTS)} may. "
        f"Route the call through the Broker interface instead (sdd.md ADR-02, ADR-04)."
    )


@pytest.mark.parametrize(
    "source",
    _source_files("packages"),
    ids=_relative,
)
def test_shared_packages_never_depend_on_apps(source: Path) -> None:
    """Dependencies point from apps into packages, never the other way around.

    The engine is a library: it must stay testable and reusable without booting an
    API. The moment it imports an app, that property is gone.
    """
    relative = _relative(source)

    match = APP_IMPORT.search(source.read_text(encoding="utf-8"))
    assert match is None, (
        f"{relative} imports the '{match.group(1)}' app. "
        f"Shared packages must not depend on deployable apps."
    )


@pytest.mark.parametrize(
    "source",
    _source_files("packages/engine"),
    ids=_relative,
)
def test_the_engine_never_reaches_for_the_database(source: Path) -> None:
    """The core is pure: candles in, orders out (ADR-0009).

    An engine that can read the database is an engine whose result depends on what is
    in it — and determinism (AGENTS.md §5.2) is gone the moment that is true. The same
    strategy, the same candles and the same costs must produce the same trades, whether
    the run happens today or on a restored dump next year.

    Data reaches the engine as arguments. It is never fetched.
    """
    relative = _relative(source)

    assert not DB_IMPORT.search(source.read_text(encoding="utf-8")), (
        f"{relative} imports tradeforge_db. The engine takes its inputs as arguments; "
        f"a core that queries a database is a core that cannot be replayed (ADR-0009)."
    )


@pytest.mark.parametrize("relative", SWING_MODULES)
def test_the_swing_family_never_imports_the_structure_machine(relative: str) -> None:
    """The MME9 and the Ponto Contínuo do not know what a zone is.

    The author's own words, asked whether the Ponto Contínuo's two corrections were the
    structure setup's: *"MME9 e BOS não têm nada a ver — são setups distintos"*. The
    geometries coincide in places; the coupling must not exist.

    The rule earns a test because the temptation is concrete and recent. When the bar
    patterns moved onto the average (2026-09-07), the obvious shortcut was to reuse
    `setups.ZoneEntry` for "a limit or a stop, never both" — one import away, and it would
    have dragged the region's vocabulary into a module about a moving average. `PatternOrder`
    exists instead. A docstring saying so is a docstring; this is the guard.

    `setup_factory` is deliberately not covered: naming both families is its whole job.
    """
    source = REPO_ROOT / relative
    match = STRUCTURE_IMPORT.search(source.read_text(encoding="utf-8"))
    assert match is None, (
        f"{relative} imports tradeforge_engine.setups. The swing setups and the structure "
        f"machine are separate setups ('não têm nada a ver'); share the bar arithmetic in "
        f"bar_setups.py instead of the region's own vocabulary."
    )
