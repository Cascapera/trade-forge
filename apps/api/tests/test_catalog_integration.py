"""`/catalog` over a real Postgres — the shelf, its labels, and the sweep saved beside them.

Integration rather than unit because almost everything worth being wrong about here is the
database's: a unique name under two writers, a foreign key that refuses to let a labelled
strategy vanish, and a CHECK that a grid is an object. Testing those against an in-memory
double would be testing the double.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.models import SweepTemplate, SweepTemplateItem, TemplateItemStatus

pytestmark = pytest.mark.integration


class _Queue:
    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        return None


@pytest.fixture
def client(session_factory: Callable[[], Session], settings: Settings) -> Any:
    app = create_app(settings=settings, session_factory=session_factory, arq_pool=_Queue())
    with TestClient(app) as opened:
        yield opened


def a_document(name: str) -> dict[str, Any]:
    """A setup document with two params a grid can reach, and one it cannot."""
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "M15",
        "setup": {
            "type": "structure_choch",
            "params": {"stop_buffer": 0.1, "breakeven_at_r": 2.0},
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def a_strategy(client: Any, name: str | None = None) -> str:
    created = client.post("/strategies", json=a_document(name or f"doc {uuid.uuid4()}"))
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


class TestTheLabelIsTheEntrysOwn:
    def test_an_entry_carries_a_name_the_document_does_not_have(self, client: Any) -> None:
        # ⚠️ The whole reason this table exists. The document is called `MME9-20260910-172055`,
        # which is the shape every name in this project's real database has — and the entry is
        # called what a person would say out loud. A fixture whose two names were similar would
        # agree with an implementation that read either one.
        strategy = a_strategy(client, "MME9-20260910-172055")

        created = client.post(
            "/catalog",
            json={
                "name": "9.1 sem filtro",
                "description": "A virada da média, sem nada por cima.",
                "strategy_id": strategy,
            },
        )

        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "9.1 sem filtro"
        assert body["strategy_name"] == "MME9-20260910-172055"
        # Read from the document, never from either name.
        assert body["setup"] == "structure_choch"

    def test_a_description_nobody_wrote_is_null_rather_than_empty(self, client: Any) -> None:
        created = client.post(
            "/catalog", json={"name": f"no words {uuid.uuid4()}", "strategy_id": a_strategy(client)}
        )

        assert created.status_code == 201
        # ⚠️ `None`, not `""`. "Nobody wrote one" and "somebody wrote nothing" are different
        # facts, and a screen deciding whether to show a subtitle reads exactly this.
        assert created.json()["description"] is None

    def test_two_entries_cannot_share_a_name(self, client: Any) -> None:
        name = f"9.1 com filtro {uuid.uuid4()}"
        first = a_strategy(client)
        second = a_strategy(client)

        first_entry = client.post("/catalog", json={"name": name, "strategy_id": first})
        assert first_entry.status_code == 201
        clash = client.post("/catalog", json={"name": name, "strategy_id": second})

        # A catalogue with two entries of one name is a catalogue nobody can speak about, and
        # the failure it produces is a person launching the one they did not mean.
        assert clash.status_code == 409


class TestTheGridIsSavedAndChecked:
    def test_an_entry_with_no_axes_is_one_point(self, client: Any) -> None:
        created = client.post(
            "/catalog", json={"name": f"plain {uuid.uuid4()}", "strategy_id": a_strategy(client)}
        )

        body = created.json()
        assert body["grid"] == {}
        # ⚠️ One, not zero. An entry with nothing to vary is still one backtest, and a shelf
        # that said `0 points` beside it would be describing a thing that does nothing.
        assert body["points"] == 1

    def test_the_grid_comes_back_and_carries_its_size(self, client: Any) -> None:
        created = client.post(
            "/catalog",
            json={
                "name": f"swept {uuid.uuid4()}",
                "strategy_id": a_strategy(client),
                "grid": {
                    "setup.params.stop_buffer": [0.1, 0.2, 0.3],
                    "setup.params.breakeven_at_r": [1.5, 2.0],
                },
            },
        )

        body = created.json()
        assert body["grid"]["setup.params.stop_buffer"] == [0.1, 0.2, 0.3]
        # Three by two, which is the number that decides whether a sweep is a click or an
        # afternoon. Derived from the grid, so it cannot drift from it.
        assert body["points"] == 6

    def test_an_axis_the_document_cannot_reach_is_refused_at_save_time(self, client: Any) -> None:
        # ⚠️ Refused now, not at launch. `htf` is a real parameter of this setup and is simply
        # absent from this document, so `expand` has nothing to substitute into — and an entry
        # carrying it would sit on the shelf looking fine until somebody ran it.
        refused = client.post(
            "/catalog",
            json={
                "name": f"broken {uuid.uuid4()}",
                "strategy_id": a_strategy(client),
                "grid": {"setup.params.htf": ["H4", "D1"]},
            },
        )

        assert refused.status_code == 422
        assert "htf" in refused.text

    def test_a_grid_past_the_old_cap_is_saved_whole(self, client: Any) -> None:
        # ⚠️ His decision (18/09), met on this very screen: a grid of 2034 combinations was refused
        # by the old cap of 500. No cap since — every combination is kept, and the entry says how
        # many it holds.
        saved = client.post(
            "/catalog",
            json={
                "name": f"huge {uuid.uuid4()}",
                "strategy_id": a_strategy(client),
                "grid": {
                    "setup.params.stop_buffer": [0.01 * i for i in range(1, 31)],
                    "setup.params.breakeven_at_r": [0.5 * i for i in range(1, 31)],
                },
            },
        )

        assert saved.status_code == 201, saved.text
        assert saved.json()["points"] == 900

    def test_the_database_refuses_a_grid_that_is_not_an_object(
        self, session: Session, client: Any
    ) -> None:
        # The CHECK, reached past the API — because the API is not the only writer a database
        # ever has, and an invariant enforced only by the code that writes the row is a rule a
        # psql session at three in the morning does not know about.
        strategy = a_strategy(client)
        with pytest.raises(Exception, match=r"(?i)a_grid_is_an_object_of_axes"):
            session.execute(
                text(
                    "INSERT INTO catalog_entries (id, name, strategy_id, grid) "
                    "VALUES (:id, :name, :sid, '[1, 2]'::jsonb)"
                ),
                {"id": uuid.uuid4(), "name": f"bad {uuid.uuid4()}", "sid": uuid.UUID(strategy)},
            )
        session.rollback()


class TestReadingAndRemoving:
    def test_the_shelf_reads_newest_first(self, client: Any) -> None:
        names = [f"entry {i} {uuid.uuid4()}" for i in range(3)]
        for name in names:
            created = client.post(
                "/catalog", json={"name": name, "strategy_id": a_strategy(client)}
            )
            assert created.status_code == 201

        listed = client.get("/catalog").json()

        assert listed["total"] == 3
        assert [item["name"] for item in listed["items"]] == list(reversed(names))

    def test_an_unknown_entry_is_a_404_rather_than_an_empty_one(self, client: Any) -> None:
        # ⚠️ The requests are made *before* the asserts, never inside them. `python -O` strips
        # an assert whole, so a request made in one is a request that does not happen — and a
        # test whose only action lives in its assertion becomes a test that checks nothing
        # while still passing. CodeQL names this `py/side-effect-in-assert`; it caught two of
        # these here, and the rest of the file is written the same way for the same reason.
        read = client.get(f"/catalog/{uuid.uuid4()}")
        removed = client.delete(f"/catalog/{uuid.uuid4()}")

        assert read.status_code == 404
        assert removed.status_code == 404

    def test_an_unknown_strategy_is_a_404_and_writes_nothing(self, client: Any) -> None:
        refused = client.post(
            "/catalog", json={"name": f"orphan {uuid.uuid4()}", "strategy_id": str(uuid.uuid4())}
        )

        listed = client.get("/catalog").json()

        assert refused.status_code == 404
        assert listed["total"] == 0

    def test_removing_an_entry_leaves_its_strategy_alone(self, client: Any) -> None:
        strategy = a_strategy(client)
        entry = client.post(
            "/catalog", json={"name": f"gone {uuid.uuid4()}", "strategy_id": strategy}
        ).json()["id"]

        removed = client.delete(f"/catalog/{entry}")
        gone = client.get(f"/catalog/{entry}")
        document = client.get(f"/strategies/{strategy}")

        assert removed.status_code == 204
        assert gone.status_code == 404
        # ⚠️ The document survives. A run answers "what did I execute?" by pointing at an
        # immutable document, so deleting one would make a finished result unexplainable —
        # what the entry owned was the label and the grid, never the strategy.
        assert document.status_code == 200

    def test_a_labelled_strategy_cannot_be_deleted_out_from_under_the_shelf(
        self, session: Session, client: Any
    ) -> None:
        # RESTRICT, proved rather than assumed: the FK is the only thing standing between a
        # shelf and a label pointing at nothing.
        strategy = a_strategy(client)
        client.post("/catalog", json={"name": f"held {uuid.uuid4()}", "strategy_id": strategy})

        with pytest.raises(Exception, match=r"(?i)fk_catalog_entries_strategy_id_strategies"):
            session.execute(
                text("DELETE FROM strategies WHERE id = :id"), {"id": uuid.UUID(strategy)}
            )
        session.rollback()


def an_entry_of(client: Any, name: str, grid: dict[str, Any] | None = None) -> dict[str, Any]:
    created = client.post(
        "/catalog",
        json={
            "name": name,
            "description": "written once",
            "strategy_id": a_strategy(client),
            "grid": grid or {},
        },
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


class TestEditingAnEntryInPlace:
    """29/09, his ask: an entry's name, description and grid change in place; its strategy does
    not — that moves by saving a new version in the builder."""

    def test_only_what_is_sent_changes(self, client: Any) -> None:
        entry = an_entry_of(client, f"before {uuid.uuid4()}", {"setup.params.stop_buffer": [0.1]})
        renamed = f"after {uuid.uuid4()}"

        edited = client.patch(f"/catalog/{entry['id']}", json={"name": renamed})

        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["name"] == renamed
        assert (body["description"], body["grid"], body["strategy_id"]) == (
            entry["description"],
            entry["grid"],
            entry["strategy_id"],
        )
        assert client.get(f"/catalog/{entry['id']}").json()["name"] == renamed

    def test_a_null_description_clears_it_and_an_absent_one_keeps_it(self, client: Any) -> None:
        entry = an_entry_of(client, f"described {uuid.uuid4()}")

        kept = client.patch(f"/catalog/{entry['id']}", json={"grid": {}}).json()
        cleared = client.patch(f"/catalog/{entry['id']}", json={"description": None}).json()

        assert kept["description"] == "written once"
        assert cleared["description"] is None

    def test_the_grid_changes_and_its_size_follows(self, client: Any) -> None:
        entry = an_entry_of(client, f"grid {uuid.uuid4()}")

        edited = client.patch(
            f"/catalog/{entry['id']}",
            json={"grid": {"setup.params.stop_buffer": [0.1, 0.2, 0.3]}},
        )

        assert edited.status_code == 200, edited.text
        assert edited.json()["points"] == 3

    def test_a_grid_the_document_cannot_reach_is_refused_and_nothing_changes(
        self, client: Any
    ) -> None:
        entry = an_entry_of(client, f"reach {uuid.uuid4()}", {"setup.params.stop_buffer": [0.1]})

        refused = client.patch(
            f"/catalog/{entry['id']}", json={"grid": {"setup.params.nowhere": [1, 2]}}
        )

        assert refused.status_code == 422
        assert client.get(f"/catalog/{entry['id']}").json()["grid"] == entry["grid"]

    def test_a_name_or_a_grid_cannot_be_null(self, client: Any) -> None:
        entry = an_entry_of(client, f"nulls {uuid.uuid4()}")

        assert client.patch(f"/catalog/{entry['id']}", json={"name": None}).status_code == 422
        assert client.patch(f"/catalog/{entry['id']}", json={"grid": None}).status_code == 422

    def test_a_name_another_entry_has_is_a_409_and_nothing_changes(self, client: Any) -> None:
        first = an_entry_of(client, f"first {uuid.uuid4()}")
        second = an_entry_of(client, f"second {uuid.uuid4()}")

        taken = client.patch(
            f"/catalog/{second['id']}", json={"name": first["name"], "description": "new"}
        )

        assert taken.status_code == 409
        again = client.get(f"/catalog/{second['id']}").json()
        assert (again["name"], again["description"]) == (second["name"], "written once")

    def test_the_strategy_is_not_something_an_edit_can_send(self, client: Any) -> None:
        entry = an_entry_of(client, f"pinned {uuid.uuid4()}")

        moved = client.patch(f"/catalog/{entry['id']}", json={"strategy_id": a_strategy(client)})

        assert moved.status_code == 422

    def test_an_unknown_entry_is_a_404(self, client: Any) -> None:
        assert client.patch(f"/catalog/{uuid.uuid4()}", json={"name": "x"}).status_code == 404

    def test_the_grid_waits_for_a_template_s_queue_and_the_label_does_not(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """A queued market launches later and reads the grid then: an edit mid-queue would split
        one template over two grids. The name is a label, and changes whatever the queue holds;
        sending the same grid back changes nothing, so it is not refused either."""
        entry = an_entry_of(client, f"queued {uuid.uuid4()}", {"setup.params.stop_buffer": [0.1]})
        template_name = f"template {uuid.uuid4()}"
        with session_factory() as session:
            template = SweepTemplate(
                name=template_name,
                entry_ids=[entry["id"]],
                timeframes=["H1"],
                date_from=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
                date_to=dt.datetime(2024, 6, 1, tzinfo=dt.UTC),
                initial_capital=Decimal(10000),
            )
            item = SweepTemplateItem(
                template=template,
                symbol="EURUSD",
                timeframe="H1",
                cost_model={"type": "none"},
                position=0,
                status=TemplateItemStatus.WAITING,
            )
            session.add_all([template, item])
            session.commit()
            item_id = item.id
        other_grid = {"setup.params.stop_buffer": [0.1, 0.2]}

        waiting = client.patch(f"/catalog/{entry['id']}", json={"grid": other_grid})
        same_grid = client.patch(f"/catalog/{entry['id']}", json={"grid": entry["grid"]})
        renamed = client.patch(f"/catalog/{entry['id']}", json={"name": f"r {uuid.uuid4()}"})

        assert waiting.status_code == 409
        assert template_name in waiting.json()["detail"]
        assert same_grid.status_code == 200
        assert renamed.status_code == 200
        with session_factory() as session:
            queued = session.get(SweepTemplateItem, item_id)
            assert queued is not None
            queued.status = TemplateItemStatus.REMOVED
            session.commit()
        assert client.patch(f"/catalog/{entry['id']}", json={"grid": other_grid}).status_code == 200
