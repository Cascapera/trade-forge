"""The signals screen by asset (ADR-0033): asset → timeframe → setups, and a market's restart.

The screen used to read setup by setup. It now reads the way the sessions run: an asset, its
markets (one per timeframe — one bar stream, one process), and in each the setups followed on it,
with their switch, their session's state, the signals under way and their record on that market
alone. The setups and their signals come from the database; what runs where comes from the
supervisor's own word in Redis (`live.fleet.SUPERVISOR_STATE_KEY`), which lapses within a minute
of it stopping — so a screen that says the supervisor is alive is never repeating an old answer.

**Restart** asks the supervisor, through Redis, to stop every session of one market and launch
them again together. It does not restart the supervisor: the API runs in Docker and cannot start a
process on the Windows host, and restarting a market's sessions is what the button is for — to
bring a setup added since the last restart into its market's process, or to unstick one.
"""

import datetime as dt
import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.exceptions import RedisError
from redis.typing import KeyT, ResponseT

from tradeforge_api.deps import SessionDep
from tradeforge_api.live.fleet import RESTARTS_KEY, SUPERVISOR_STATE_KEY, restart_field
from tradeforge_api.schemas import (
    LiveAssetOut,
    LiveMarketSetupOut,
    LiveMarketsOut,
    LiveMetricsOut,
    LiveSessionState,
    LiveTimeframeOut,
    MarketProcessOut,
    RestartMarketRequest,
    SignalOut,
    SupervisorOut,
)
from tradeforge_db.live_setups import SetupView, list_setups, setup_metrics, signals_under_way
from tradeforge_schema.models import TIMEFRAMES

__all__ = ["SignalsStore", "router"]

router = APIRouter(tags=["live-markets"])


class SignalsStore(Protocol):
    """The two Redis calls this screen makes, in redis-py's own vocabulary (see `StopStore`)."""

    def get(self, name: KeyT) -> ResponseT: ...

    def hset(self, name: str, key: str, value: str) -> ResponseT: ...


def get_signals_store(request: Request) -> SignalsStore:
    store: SignalsStore = request.app.state.signals_store
    return store


SignalsStoreDep = Annotated[SignalsStore, Depends(get_signals_store)]


@router.get("/live-markets", response_model=LiveMarketsOut)
def markets(session: SessionDep, store: SignalsStoreDep) -> LiveMarketsOut:
    """Every asset followed, its markets, and each setup on them, with the supervisor's word."""
    try:
        raw = store.get(SUPERVISOR_STATE_KEY)
        reachable = True
    except RedisError:
        raw, reachable = None, False
    word = _word(raw)
    supervisor = SupervisorOut(
        alive=word is not None,
        at=_instant(word.get("at")) if word is not None else None,
        reachable=reachable,
    )
    under_way = signals_under_way(session)

    assets: dict[uuid.UUID, dict[str, Any]] = {}
    for view in list_setups(session):
        for market in view.markets:
            asset = assets.setdefault(
                market.instrument_id,
                {"symbol": market.symbol, "broker": market.broker, "timeframes": {}},
            )
            rows: list[LiveMarketSetupOut] = asset["timeframes"].setdefault(
                view.setup.timeframe, []
            )
            rows.append(
                _setup_row(
                    session,
                    view,
                    instrument_id=market.instrument_id,
                    market_active=market.active,
                    under_way=under_way.get((view.setup.id, market.instrument_id), []),
                    word=word,
                )
            )

    order = {timeframe: index for index, timeframe in enumerate(TIMEFRAMES)}
    return LiveMarketsOut(
        supervisor=supervisor,
        assets=[
            LiveAssetOut(
                instrument_id=instrument_id,
                symbol=asset["symbol"],
                broker=asset["broker"],
                timeframes=[
                    LiveTimeframeOut(
                        timeframe=timeframe,
                        processes=_processes(word, instrument_id, timeframe),
                        restarting=_restarting(word, instrument_id, timeframe),
                        setups=sorted(rows, key=lambda row: row.name.casefold()),
                    )
                    for timeframe, rows in sorted(
                        asset["timeframes"].items(), key=lambda pair: order.get(pair[0], 99)
                    )
                ],
            )
            for instrument_id, asset in sorted(
                assets.items(), key=lambda pair: str(pair[1]["symbol"]).casefold()
            )
        ],
    )


@router.post("/live-markets/restart", status_code=status.HTTP_202_ACCEPTED)
def restart(request: RestartMarketRequest, store: SignalsStoreDep) -> dict[str, bool]:
    """Ask the supervisor to restart one market's sessions together (taken within one round; a
    request it has not taken within `fleet.RESTART_TTL` is dropped).

    ⚠️ Every signal armed on that market is cancelled on the channel and every one in a trade is
    then followed by price (`live/orphans.py`): the screen says how many before it asks.
    """
    try:
        store.hset(
            RESTARTS_KEY,
            restart_field(request.instrument_id, request.timeframe),
            dt.datetime.now(dt.UTC).isoformat(),
        )
    except RedisError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"could not reach Redis to ask for it ({error})"
        ) from error
    return {"requested": True}


def _word(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, str | bytes) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _instant(raw: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _of(word: Mapping[str, Any] | None, field: str) -> list[dict[str, Any]]:
    found = [] if word is None else word.get(field, [])
    return [one for one in found if isinstance(one, dict)] if isinstance(found, list) else []


def _processes(
    word: Mapping[str, Any] | None, instrument_id: uuid.UUID, timeframe: str
) -> list[MarketProcessOut]:
    return [
        MarketProcessOut(
            key=str(process.get("key", "")),
            phase=process.get("phase"),
            heard_at=_instant(process.get("heard_at")),
            sessions=len(process.get("sessions", [])),
        )
        for process in _processes_raw(word, instrument_id, timeframe)
    ]


def _restarting(word: Mapping[str, Any] | None, instrument_id: uuid.UUID, timeframe: str) -> bool:
    return any(
        one.get("instrument_id") == str(instrument_id) and one.get("timeframe") == timeframe
        for one in _of(word, "restarting")
    )


def _state(
    word: Mapping[str, Any] | None,
    setup_id: uuid.UUID,
    instrument_id: uuid.UUID,
    timeframe: str,
) -> tuple[LiveSessionState, int | None]:
    """What the supervisor says of one switched-on setup on one market; `unknown` when it says
    nothing — stopped, or Redis unreadable."""
    if word is None:
        return "unknown", None
    for waiting in _of(word, "waiting"):
        if waiting.get("setup_id") == str(setup_id) and waiting.get("instrument_id") == str(
            instrument_id
        ):
            left = waiting.get("retry_in")
            return "waiting", int(left) if isinstance(left, int | float) else None
    for process in _processes_raw(word, instrument_id, timeframe):
        for one in process.get("sessions", []):
            if isinstance(one, dict) and one.get("setup_id") == str(setup_id):
                if one.get("asked"):
                    return "stopping", None
                phase = process.get("phase")
                if phase == "live":
                    return "running", None
                return ("warming" if phase == "warming" else "starting"), None
    return "starting", None


def _processes_raw(
    word: Mapping[str, Any] | None, instrument_id: uuid.UUID, timeframe: str
) -> list[dict[str, Any]]:
    return [
        process
        for process in _of(word, "processes")
        if process.get("instrument_id") == str(instrument_id)
        and process.get("timeframe") == timeframe
    ]


def _setup_row(  # noqa: PLR0913 — one setup on one market, and what the supervisor says of it
    session: SessionDep,
    view: SetupView,
    *,
    instrument_id: uuid.UUID,
    market_active: bool,
    under_way: list[Any],
    word: Mapping[str, Any] | None,
) -> LiveMarketSetupOut:
    setup = view.setup
    state: LiveSessionState = "off"
    retry_in = None
    if setup.active and market_active:
        state, retry_in = _state(word, setup.id, instrument_id, setup.timeframe)
    metrics = setup_metrics(session, setup.id, instrument_id=instrument_id)
    return LiveMarketSetupOut(
        setup_id=setup.id,
        name=setup.name,
        strategy_name=view.strategy_name,
        setup_active=setup.active,
        active=market_active,
        no_target_r=setup.no_target_r,
        state=state,
        retry_in=retry_in,
        under_way=[SignalOut.model_validate(one) for one in under_way],
        metrics=LiveMetricsOut(
            signals=metrics.signals,
            closed=metrics.closed,
            open=metrics.open,
            cancelled=metrics.cancelled,
            wins=metrics.wins,
            win_rate=metrics.win_rate,
            net_r=metrics.net_r,
            average_r=metrics.average_r,
            profit_factor=metrics.profit_factor,
            max_drawdown_r=metrics.max_drawdown_r,
            r_by_month=cast("dict[str, Any]", metrics.r_by_month),
        ),
    )
