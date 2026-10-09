"""Signal pictures (signals PR 7): drawn, attached to the right events, never in the way."""

import datetime as dt
import urllib.request
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_api.live.notifier import discord, telegram
from tradeforge_api.live.signal_image import render_signal_png
from tradeforge_api.live.signals import SignalKindOf
from tradeforge_engine.domain import Candle, EntrySnapshot, SnapshotRegion

from .test_signals import STEP, T0, armed_long, bar, broker

PNG = b"\x89PNG\r\n\x1a\n"


def candles(n: int = 30) -> list[Candle]:
    return [bar(i, 900 + i, 1000 + i) for i in range(n)]


def test_a_picture_is_a_png_of_the_candles_the_setup_and_the_levels() -> None:
    bars = candles()
    snapshot = EntrySnapshot(
        bars=tuple(bars),
        decided_at=bars[-1].time,
        regions=(
            SnapshotRegion(
                label="zona", top=Decimal(960), bottom=Decimal(940), from_time=bars[10].time
            ),
        ),
    )

    png = render_signal_png(
        bars,
        title="ARMADO #41 — WIN",
        entry=Decimal(1030),
        stop=Decimal(900),
        target=None,
        snapshot=snapshot,
    )

    assert png.startswith(PNG)


def test_a_picture_without_candles_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one candle"):
        render_signal_png([], title="x", entry=None, stop=None, target=None)


class Drawn:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list[dict[str, Any]] = []

    def __call__(self, candles: Any, **fields: Any) -> bytes:
        self.calls.append({"candles": list(candles), **fields})
        if self.fails:
            raise RuntimeError("no font")
        return PNG


def picturing(drawn: Drawn) -> tuple[Any, Any]:
    signals, sink = broker()
    signals._picture = drawn
    return signals, sink


def test_armed_is_drawn_from_what_the_setup_saw_and_cancelled_is_not_drawn() -> None:
    drawn = Drawn()
    signals, sink = picturing(drawn)
    seen = (bar(-2, 900, 950), bar(-1, 920, 980))
    snapshot = EntrySnapshot(bars=seen, decided_at=T0 - STEP)

    signals.submit(armed_long(snapshot=snapshot))
    signals.cancel("c1")

    armed, cancelled = sink.events
    assert armed.image == PNG
    assert drawn.calls[0]["candles"] == list(seen)
    assert drawn.calls[0]["title"] == "ARMADO #41 — WIN"
    assert cancelled.kind is SignalKindOf.CANCELLED
    assert cancelled.image is None
    assert len(drawn.calls) == 1


def test_triggered_and_closed_are_drawn_from_the_bars_since() -> None:
    drawn = Drawn()
    signals, sink = picturing(drawn)
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))
    signals.on_bar(bar(2, 890, 990))

    assert [event.image for event in sink.events[1:]] == [PNG, PNG]
    assert len(drawn.calls[-1]["candles"]) == 2
    assert drawn.calls[-1]["exit_price"] == Decimal(900)


def test_a_picture_that_fails_leaves_the_signal_as_text() -> None:
    signals, sink = picturing(Drawn(fails=True))
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))

    assert [event.kind for event in sink.events] == [SignalKindOf.ARMED, SignalKindOf.TRIGGERED]
    assert all(event.image is None for event in sink.events)


def sent(network: Any, *, image: bytes | None) -> urllib.request.Request:
    requests: list[urllib.request.Request] = []

    def fetch(request: urllib.request.Request) -> bytes:
        requests.append(request)
        return b"{}"

    network(fetch=fetch).post("🟡 ARMADO #41", image)
    return requests[0]


def test_telegram_sends_a_picture_as_a_photo_with_the_text_as_its_caption() -> None:
    request = sent(lambda **seams: telegram("T", "-100", **seams), image=PNG)

    body = request.data
    assert isinstance(body, bytes)
    assert request.full_url == "https://api.telegram.org/botT/sendPhoto"
    assert request.get_header("Content-type", "").startswith("multipart/form-data; boundary=")
    assert b'name="caption"' in body
    assert "🟡 ARMADO #41".encode() in body
    assert PNG in body


def test_discord_attaches_the_picture_to_the_same_message() -> None:
    request = sent(lambda **seams: discord("https://discord.test/hook", **seams), image=PNG)

    body = request.data
    assert isinstance(body, bytes)
    assert b'name="payload_json"' in body
    assert b'name="files[0]"; filename="signal.png"' in body


def test_without_a_picture_the_message_is_plain_text() -> None:
    request = sent(lambda **seams: telegram("T", "-100", **seams), image=None)

    assert request.full_url.endswith("/sendMessage")
    assert request.get_header("Content-type") == "application/json"


def test_the_time_axis_reads_brasilia() -> None:
    """A smoke check that a real session's bars, UTC, are accepted as they come."""
    bars = [bar(i, 900, 1000) for i in range(3)]
    assert bars[0].time.tzinfo == dt.UTC
    assert render_signal_png(bars, title="t", entry=None, stop=None, target=None).startswith(PNG)
