"""`GET /strategies` over a real Postgres — the listing, which is a query rather than plumbing.

The four things it has to get right cannot be checked without a database: that a lineage is one
row and not one per version, that a grid's own points are left out, that the run count is one
aggregate rather than an N+1, and that a name a client asks about is actually found — which is
the whole reason the builder's 409 exists.

Run locally with `docker compose up -d`, then:

    POSTGRES_DB=tradeforge_test uv run pytest -m integration

⚠️ The variable is not optional. Without it the integration suite truncates whatever database
the environment points at, which on a developer machine is the real one.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.models import Instrument
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
SYMBOL = "EURUSD"


class _CapturingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args))


def _seed_instrument(session: Session) -> None:
    session.add(
        Instrument(
            symbol=SYMBOL,
            name="Euro vs US Dollar",
            asset_class=AssetClass.FOREX,
            currency_base="EUR",
            currency_quote="USD",
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            contract_size=Decimal("100000"),
            digits=5,
        )
    )
    session.commit()


def _app(settings: Settings, session_factory: Callable[[], Session], tmp_path: Path) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )


def _document(name: str, period: int = 9) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "H1",
        "setup": {
            "type": "mme9_breakout",
            "params": {"side": "long", "period": period, "breakeven_at_r": 2.0},
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _create(client: TestClient, name: str, period: int = 9) -> str:
    response = client.post("/strategies", json=_document(name, period))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _launch_study(client: TestClient, strategy_id: str) -> dict[str, Any]:
    response = client.post(
        "/studies",
        json={
            "strategy_id": strategy_id,
            "symbol": SYMBOL,
            "timeframe": "H1",
            "date_from": START.isoformat(),
            "date_to": (START + 100 * HOUR).isoformat(),
            "initial_capital": "10000",
            "cost_model": {"type": "none"},
            "grid": {"setup.params.period": [5, 9, 20]},
        },
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_lineage_is_one_row_however_many_times_it_was_edited(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """A strategy edited three times is three rows and **one** strategy.

    Listing all of them would make the reader choose a version to answer a question about a
    method — and the answer they want is always the latest, which is the one a new run uses.
    """
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        first = _create(client, "MME9 lineage", period=5)
        second = client.put(f"/strategies/{first}", json=_document("MME9 lineage", period=9))
        assert second.status_code == 201, second.text
        third = client.put(
            f"/strategies/{second.json()['id']}", json=_document("MME9 lineage", period=20)
        )
        assert third.status_code == 201, third.text

        body = client.get("/strategies").json()

    assert body["total"] == 1
    [item] = body["items"]
    assert item["version"] == 3
    # The id is the *latest* version's, not the lineage's first — a client launching from this
    # row must run what the strategy is now, not what it was three edits ago.
    assert item["id"] == third.json()["id"]


def test_a_grids_own_points_are_left_out_of_the_picker(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ Without this the list is unusable the first time anybody searches a parameter space.

    A study writes one strategy per combination, so a three-point grid adds three rows and a
    hundred-point grid adds a hundred. They are real strategies and they are reproducible, but
    nobody picks `MME9 [period=5]` from a list by hand — and burying the authored ones under
    them would break the only thing this endpoint is for.

    Which ones those are is **derived**: a point is a strategy whose runs belong to a study.
    Asserted through the endpoint rather than by counting rows, because the derivation is the
    part that can be wrong.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        base = _create(client, "MME9 base")
        study = _launch_study(client, base)

        default = client.get("/strategies").json()
        everything = client.get("/strategies", params={"include_generated": True}).json()

    assert len(study["points"]) == 3
    # Only the authored one, though four strategies exist.
    assert default["total"] == 1
    assert [item["name"] for item in default["items"]] == ["MME9 base"]
    # And the points are findable for a reader who wants the exact document a grid ran.
    assert everything["total"] == 4


def test_the_run_count_says_which_strategy_has_been_worked_on(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """What distinguishes a strategy from something typed once and abandoned.

    ⚠️ Two strategies with **different** counts, because a query returning the same number for
    every row — the whole table's count, a constant — would satisfy any assertion made on one
    of them alone.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        busy = _create(client, "Run twice")
        _create(client, "Never run")
        for _ in range(2):
            response = client.post(
                "/backtests",
                json={
                    "strategy_id": busy,
                    "symbol": SYMBOL,
                    "timeframe": "H1",
                    "date_from": START.isoformat(),
                    "date_to": (START + 100 * HOUR).isoformat(),
                    "initial_capital": "10000",
                    "cost_model": {"type": "none"},
                },
            )
            assert response.status_code == 202, response.text

        body = client.get("/strategies").json()

    counts = {item["name"]: item["runs"] for item in body["items"]}
    assert counts == {"Run twice": 2, "Never run": 0}


def test_a_name_can_be_looked_up_which_is_what_the_builder_lacked(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The 409's actual cause, stated as the capability that was missing.

    With no way to ask whether a name is taken, the builder chooses between `POST` and `PUT`
    from the only id it knows — the one it created in this browser session — so a name saved
    from any other session is invisible to it and saving under it collides.
    """
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        _create(client, "Ponto Continuo H4")
        _create(client, "MME9 breakout")

        found = client.get("/strategies", params={"q": "ponto"}).json()
        missing = client.get("/strategies", params={"q": "nothing like this"}).json()

    # Case-insensitive substring: a reader typing part of a name should find it, and the
    # builder asking about an exact one finds it too.
    assert [item["name"] for item in found["items"]] == ["Ponto Continuo H4"]
    assert missing["total"] == 0
    assert missing["items"] == []


def test_the_row_says_which_setup_a_strategy_runs_because_the_name_can_lie(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ Not hypothetical: this project's own database holds a strategy called
    `Structure — CHoCH 56454` that runs `mme9_breakout`.

    A name is typed by a person and a setup is executed by the engine, so only one of them is
    evidence. The row carries the setup read from the document itself, never a copy stored
    beside it — projecting it into a column would create a second answer that could drift from
    the strategy it describes.
    """
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        _create(client, "Structure - CHoCH but not really")
        body = client.get("/strategies").json()

    [item] = body["items"]
    assert item["name"] == "Structure - CHoCH but not really"
    assert item["setup"] == "mme9_breakout"


def test_the_page_reports_a_total_that_survives_paging(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """`total` counts lineages, not the rows on this page — a client sizes its pager from it.

    ⚠️ The subtlety worth pinning: the total must be counted **after** the exclusions, or a
    pager would offer pages that come back empty because the rows it counted were filtered out
    of every one of them.
    """
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        for at in range(5):
            _create(client, f"Strategy {at}")

        page = client.get("/strategies", params={"limit": 2, "offset": 2}).json()

    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 2
    assert len(page["items"]) == 2


def test_an_exact_name_can_be_asked_about_separately_from_a_search(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The builder's question is "is *this* name taken", which a substring cannot answer.

    Three names where one contains another: searching `MME9` finds all three, and only the
    exact lookup answers the question the builder is actually asking. Without this the screen
    would have to filter in the client and hope the match landed on the first page — the same
    shape of guess that produces the 409 this endpoint exists to remove.

    ⚠️ The exact lookup also has to see a **grid's own points**, which the default listing
    hides. A name colliding with one of those collides just as hard in the database, and a
    builder told the name was free would meet the 409 it was trying to avoid.
    """
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        _create(client, "MME9")
        _create(client, "MME9 breakout")
        _create(client, "MME9 breakout H4")

        search = client.get("/strategies", params={"q": "MME9"}).json()
        exact = client.get("/strategies", params={"name": "MME9 breakout"}).json()
        free = client.get("/strategies", params={"name": "Never used"}).json()

    assert search["total"] == 3
    assert [item["name"] for item in exact["items"]] == ["MME9 breakout"]
    assert free["total"] == 0


def test_the_exact_lookup_finds_a_name_a_grid_generated(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """A point's name is taken in the database whether or not a picker chooses to show it.

    So the exact lookup asks with `include_generated`, and this pins that: hidden from the list
    is not the same as free to reuse, and conflating them puts the 409 back.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        base = _create(client, "MME9 base")
        _launch_study(client, base)

        hidden = client.get("/strategies", params={"name": "MME9 base [period=5]"}).json()
        shown = client.get(
            "/strategies",
            params={"name": "MME9 base [period=5]", "include_generated": True},
        ).json()

    # Hidden by default, exactly as the picker wants…
    assert hidden["total"] == 0
    # …and findable when asked, which is what keeps a name from being offered twice.
    assert [item["name"] for item in shown["items"]] == ["MME9 base [period=5]"]
