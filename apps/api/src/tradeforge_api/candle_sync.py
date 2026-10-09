"""Candles for the workers on other machines (09/10): pulled from the main API, never pushed.

His ask, after a sweep of Brazilian shares failed 99 308 runs in a morning because the Xeon,
Contabo and AWS had never been sent the new candles:

* **every hour** each remote machine pulls what is new or changed (`python -m
  tradeforge_api.candle_sync --every 3600`) — a new market, this year's bars collected again;
* **and on demand**, between two syncs: a worker asked for a series it does not have, or whose
  last year it lacks, fetches just that series before reading (`FetchingReader`). The run that
  found the gap pays one download (a couple of MB); every later run reads the local copy.

One code path for both — `pull` — so the two cannot disagree about what "up to date" means: a
file is fetched when it is missing locally, or its size or modification time differs from the
main machine's. Files are written to a temporary name and renamed, so a reader never sees half.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tradeforge_api.candle_cache import CandleReader
from tradeforge_collector import Candle

__all__ = ["FetchingReader", "RemoteFile", "main", "pull", "remote_files"]

logger = logging.getLogger(__name__)

URL_ENV = "TRADEFORGE_CANDLES_URL"
"""The main API, e.g. `http://100.64.155.28:8000`. Set only on remote workers; unset, a worker
reads its own disk exactly as before."""

_RETRY_AFTER = 600.0
"""Seconds before asking again for a series the main machine did not have either."""


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One Parquet file on the main machine: where it sits under the root, and its stamp."""

    path: str
    size: int
    modified: float


Fetch = Callable[[str], bytes]


_FULL_LISTING_TIMEOUT = 180
"""The hourly sync's whole listing: measured 23 s for 6 156 files on 09/10 — the main machine's
Docker reads its Windows disk slowly — and it only grows. Nothing waits on it but the sync."""

_TIMEOUT = 15
"""A file, or one series' listing: what a run waits on. With the main machine switched off, a run
waits this long at most before going on with its own disk (his point, 09/10)."""


def _get(url: str) -> bytes:
    full_listing = urllib.parse.urlparse(url).path.endswith("/candle-files") and "?" not in url
    timeout = _FULL_LISTING_TIMEOUT if full_listing else _TIMEOUT
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — our own API
        return bytes(response.read())


def remote_files(
    base_url: str,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    fetch: Fetch = _get,
) -> list[RemoteFile]:
    """What the main machine holds, optionally for one series."""
    query = urllib.parse.urlencode(
        {key: value for key, value in (("symbol", symbol), ("timeframe", timeframe)) if value}
    )
    body = fetch(f"{base_url.rstrip('/')}/candle-files{'?' + query if query else ''}")
    return [RemoteFile(**one) for one in json.loads(body)]


def _stale(root: Path, one: RemoteFile) -> bool:
    local = root / one.path
    if not local.exists():
        return True
    stat = local.stat()
    return stat.st_size != one.size or stat.st_mtime < one.modified - 1


def pull(base_url: str, root: Path, files: Sequence[RemoteFile], *, fetch: Fetch = _get) -> int:
    """Fetch the files that are missing or differ locally; how many it fetched."""
    fetched = 0
    for one in files:
        if not _stale(root, one):
            continue
        target = root / one.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = fetch(f"{base_url.rstrip('/')}/candle-files/{urllib.parse.quote(one.path)}")
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(data)
        partial.replace(target)
        os.utime(target, (one.modified, one.modified))
        fetched += 1
    return fetched


class FetchingReader:
    """A `CandleReader` that fetches a series from the main machine when the disk lacks it.

    Fetched when the series' folder is missing, or when the run reaches into a year this machine
    has no file for — the case of a market collected again after the last hourly sync. A gap
    inside a year this machine already has waits for the next sync.
    """

    def __init__(
        self,
        inner: CandleReader,
        base_url: str,
        *,
        fetch: Fetch = _get,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._url = base_url
        self._fetch = fetch
        self._clock = clock
        self._asked: dict[tuple[str, str], float] = {}

    def __call__(
        self,
        root: Path,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> Sequence[Candle]:
        series = root / f"symbol={symbol}" / f"timeframe={timeframe}"
        lacking = not series.exists() or (
            end is not None and not (series / f"year={end.year}").exists()
        )
        if lacking:
            self._ensure(root, symbol, timeframe)
        return self._inner(root, symbol, timeframe, start=start, end=end)

    def _ensure(self, root: Path, symbol: str, timeframe: str) -> None:
        key = (symbol, timeframe)
        last = self._asked.get(key)
        if last is not None and self._clock() - last < _RETRY_AFTER:
            return  # asked recently; the main machine had nothing newer then
        self._asked[key] = self._clock()
        try:
            files = remote_files(self._url, symbol=symbol, timeframe=timeframe, fetch=self._fetch)
            fetched = pull(self._url, root, files, fetch=self._fetch)
        except Exception:
            logger.exception("could not fetch %s %s from %s", symbol, timeframe, self._url)
            return
        if fetched:
            logger.info(
                "fetched %d file(s) of %s %s from %s", fetched, symbol, timeframe, self._url
            )


def sync_once(base_url: str, root: Path, *, fetch: Fetch = _get) -> int:
    """Bring every series up to date with the main machine; how many files it fetched."""
    return pull(base_url, root, remote_files(base_url, fetch=fetch), fetch=fetch)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-candle-sync",
        description="Pull new or changed candles from the main machine's API.",
    )
    parser.add_argument("--url", default=os.environ.get(URL_ENV), help=f"the main API ({URL_ENV})")
    parser.add_argument(
        "--root", type=Path, default=Path(os.environ.get("PARQUET_ROOT", "data/ohlcv"))
    )
    parser.add_argument(
        "--every", type=float, default=0, help="seconds between syncs; 0 syncs once and exits"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Sync once, or every `--every` seconds until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    if not args.url:
        logger.error("no main API to pull from: pass --url or set %s", URL_ENV)
        return 2
    try:
        while True:
            try:
                fetched = sync_once(args.url, args.root)
                logger.info("candle sync: %d file(s) fetched into %s", fetched, args.root)
            except Exception:
                logger.exception("candle sync failed; retrying next round")
            if args.every <= 0:
                return 0
            time.sleep(args.every)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
