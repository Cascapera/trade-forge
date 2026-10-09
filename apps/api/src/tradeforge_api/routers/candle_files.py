"""`/candle-files` — the Parquet candles, served to the workers on other machines (09/10).

Read-only, and only the files under the candle root that match the collector's own layout
(`symbol=…/timeframe=…/year=…/*.parquet`): a path that tries to climb out, or names anything
else, is refused. See `candle_sync` for who asks and when.
"""

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tradeforge_api.deps import SettingsDep

router = APIRouter(tags=["candle-files"])

_LAYOUT = re.compile(r"^symbol=[^/\\]+/timeframe=[A-Z0-9]{2,3}/year=\d{4}/[^/\\]+\.parquet$")


class CandleFileOut(BaseModel):
    path: str
    """Under the candle root, with forward slashes: `symbol=WIN/timeframe=M15/year=2024/…`."""
    size: int
    modified: float
    """Seconds since the epoch — what a copy compares to tell it is stale."""


@router.get("/candle-files", response_model=list[CandleFileOut])
def listed(
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(max_length=32, pattern=r"^[^/\\]+$")] = None,
    timeframe: Annotated[str | None, Query(max_length=3, pattern=r"^[A-Z0-9]+$")] = None,
) -> list[CandleFileOut]:
    """Every candle file, or one series' files, with its size and modification time."""
    root = settings.parquet_root
    pattern = f"symbol={symbol or '*'}/timeframe={timeframe or '*'}/year=*/*.parquet"
    found = []
    for file in sorted(root.glob(pattern)):
        relative = file.relative_to(root).as_posix()
        if _LAYOUT.match(relative):
            stat = file.stat()
            found.append(CandleFileOut(path=relative, size=stat.st_size, modified=stat.st_mtime))
    return found


@router.get(
    "/candle-files/{path:path}",
    response_class=FileResponse,
    responses={status.HTTP_404_NOT_FOUND: {"description": "no such candle file"}},
)
def served(settings: SettingsDep, path: str) -> FileResponse:
    """One candle file, as bytes."""
    if not _LAYOUT.match(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not a candle file")
    root = settings.parquet_root.resolve()
    file = (root / path).resolve()
    if root not in file.parents or not file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such candle file")
    return FileResponse(file, media_type="application/octet-stream")
