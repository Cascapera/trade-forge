"""Candles pulled to the workers on other machines (09/10): hourly, and on demand."""

import datetime as dt
import json
import urllib.parse
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tradeforge_api.candle_sync import FetchingReader, pull, remote_files, sync_once
from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_collector import Candle

BASE = "http://main:8000"
PETR4_2024 = "symbol=PETR4/timeframe=H1/year=2024/part-0.parquet"
PETR4_2026 = "symbol=PETR4/timeframe=H1/year=2026/part-0.parquet"
WIN_2026 = "symbol=WIN/timeframe=H1/year=2026/part-0.parquet"


class Main:
    """The main machine's API, faked: a listing and the files' bytes."""

    def __init__(self, files: dict[str, bytes], modified: float = 1_700_000_000.0) -> None:
        self.files = files
        self.modified = modified
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/candle-files":
            query = urllib.parse.parse_qs(parsed.query)
            prefix = ""
            if "symbol" in query:
                prefix = f"symbol={query['symbol'][0]}/timeframe={query['timeframe'][0]}/"
            return json.dumps(
                [
                    {"path": path, "size": len(data), "modified": self.modified}
                    for path, data in self.files.items()
                    if path.startswith(prefix)
                ]
            ).encode()
        return self.files[urllib.parse.unquote(parsed.path.removeprefix("/candle-files/"))]


def test_a_sync_fetches_what_is_missing_and_only_that(tmp_path: Path) -> None:
    main = Main({PETR4_2024: b"old year", WIN_2026: b"new market"})

    assert sync_once(BASE, tmp_path, fetch=main) == 2
    assert (tmp_path / WIN_2026).read_bytes() == b"new market"

    main.asked.clear()
    assert sync_once(BASE, tmp_path, fetch=main) == 0, "nothing changed: nothing fetched"
    assert all("/candle-files?" in url or url.endswith("/candle-files") for url in main.asked)


def test_a_file_changed_on_the_main_machine_is_fetched_again(tmp_path: Path) -> None:
    main = Main({PETR4_2026: b"bars until yesterday"})
    sync_once(BASE, tmp_path, fetch=main)

    main.files[PETR4_2026] = b"bars until today, longer"
    main.modified += 3600

    assert sync_once(BASE, tmp_path, fetch=main) == 1
    assert (tmp_path / PETR4_2026).read_bytes() == b"bars until today, longer"
    assert not list(tmp_path.rglob("*.part")), "the temporary file is renamed, never left"


def test_one_series_can_be_listed_alone(tmp_path: Path) -> None:
    main = Main({PETR4_2024: b"a", WIN_2026: b"b"})

    files = remote_files(BASE, symbol="WIN", timeframe="H1", fetch=main)

    assert [one.path for one in files] == [WIN_2026]
    assert pull(BASE, tmp_path, files, fetch=main) == 1


class Disk:
    """The worker's own reader, faked: records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        root: Path,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> Sequence[Candle]:
        self.calls.append((symbol, timeframe))
        return []


def test_a_worker_missing_a_series_fetches_it_before_reading(tmp_path: Path) -> None:
    main = Main({WIN_2026: b"bars"})
    disk = Disk()
    read = FetchingReader(disk, BASE, fetch=main)

    read(tmp_path, "WIN", "H1", end=dt.datetime(2026, 10, 8, tzinfo=dt.UTC))

    assert (tmp_path / WIN_2026).exists()
    assert disk.calls == [("WIN", "H1")]


def test_a_worker_missing_the_runs_last_year_fetches_it(tmp_path: Path) -> None:
    main = Main({PETR4_2024: b"2024", PETR4_2026: b"2026"})
    (tmp_path / PETR4_2024).parent.mkdir(parents=True)
    (tmp_path / PETR4_2024).write_bytes(b"2024")
    read = FetchingReader(Disk(), BASE, fetch=main)

    read(tmp_path, "PETR4", "H1", end=dt.datetime(2026, 1, 5, tzinfo=dt.UTC))

    assert (tmp_path / PETR4_2026).read_bytes() == b"2026"


def test_a_worker_with_the_series_does_not_ask(tmp_path: Path) -> None:
    main = Main({WIN_2026: b"bars"})
    (tmp_path / WIN_2026).parent.mkdir(parents=True)
    (tmp_path / WIN_2026).write_bytes(b"bars")
    read = FetchingReader(Disk(), BASE, fetch=main)

    read(tmp_path, "WIN", "H1", end=dt.datetime(2026, 10, 8, tzinfo=dt.UTC))

    assert main.asked == []


def test_a_series_the_main_machine_lacks_too_is_not_asked_again_at_once(tmp_path: Path) -> None:
    main = Main({})
    now = [0.0]
    read = FetchingReader(Disk(), BASE, fetch=main, clock=lambda: now[0])

    read(tmp_path, "NOPE", "H1")
    read(tmp_path, "NOPE", "H1")
    assert len(main.asked) == 1
    now[0] = 601.0
    read(tmp_path, "NOPE", "H1")
    assert len(main.asked) == 2


def test_the_main_machine_being_away_leaves_the_run_on_its_own_disk(tmp_path: Path) -> None:
    def away(url: str) -> bytes:
        raise OSError("connection refused")

    disk = Disk()
    FetchingReader(disk, BASE, fetch=away)(tmp_path, "WIN", "H1")

    assert disk.calls == [("WIN", "H1")]


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PARQUET_ROOT", str(tmp_path))
    for path, data in ((PETR4_2024, b"x" * 10), (WIN_2026, b"y" * 3)):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(data)
    (tmp_path / "notes.txt").write_text("not a candle")
    app: Any = create_app(settings=Settings())
    with TestClient(app) as client:
        yield client


def test_the_api_lists_and_serves_the_candle_files(api: TestClient) -> None:
    listed = api.get("/candle-files").json()
    one = api.get("/candle-files", params={"symbol": "WIN", "timeframe": "H1"}).json()

    assert [(f["path"], f["size"]) for f in listed] == [(PETR4_2024, 10), (WIN_2026, 3)]
    assert [f["path"] for f in one] == [WIN_2026]
    assert api.get(f"/candle-files/{WIN_2026}").content == b"yyy"


@pytest.mark.parametrize(
    "path",
    [
        "notes.txt",
        "symbol=WIN/timeframe=H1/year=2026/../../../notes.txt",
        "symbol=WIN/timeframe=H1/year=2026/missing.parquet",
    ],
)
def test_the_api_serves_nothing_but_candle_files(api: TestClient, path: str) -> None:
    assert api.get(f"/candle-files/{path}").status_code == 404
