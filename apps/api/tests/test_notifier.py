"""The notifier: one message per event, the disclaimer on each, never posted twice (PR 6)."""

import io
import json
import logging
import urllib.error
import urllib.request

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from tradeforge_api.live.notifier import (
    DISCLAIMER,
    OUTAGE_PAUSE,
    deliver,
    discord,
    format_message,
    serve_once,
    telegram,
)
from tradeforge_api.live.outage import Outage
from tradeforge_api.live.signals import SIGNALS_STREAM

ARMED = {
    "kind": "armed",
    "number": "41",
    "symbol": "WIN",
    "timeframe": "H1",
    "strategy": "CHOCH BASE",
    "broker": "xp",
    "side": "long",
    "order_type": "stop",
    "entry": "207055",
    "stop": "206500.0",
    "time": "2026-10-08T19:00:00+00:00",
    "no_target_r": "5",
}


def test_an_armed_signal_says_where_it_waits_and_how_it_ends() -> None:
    text = format_message(ARMED)

    assert text.splitlines()[0] == "🟡 ARMADO #41 — WIN H1"
    assert "CHOCH BASE · xp" in text
    assert "Compra (stop) em 207.055" in text
    assert "Stop 206.500" in text
    assert "Sem alvo: encerra em 5R, no stop ou na saída do setup" in text
    assert "08/10 17:00 (Brasília) · candle H1 das 16:00" in text
    assert text.endswith(f"_{DISCLAIMER}_")


def test_the_time_is_the_close_of_the_candle_that_decided() -> None:
    """09/10: "14:20" on a signal that arrived at 14:30 read as a wrong clock — it was the open."""
    text = format_message({**ARMED, "timeframe": "M5", "time": "2026-10-09T17:20:00+00:00"})
    assert "09/10 14:25 (Brasília) · candle M5 das 14:20" in text


def test_a_timeframe_it_cannot_read_falls_back_to_the_stamp() -> None:
    text = format_message({**ARMED, "timeframe": "", "time": "2026-10-09T17:20:00+00:00"})
    assert "09/10 14:20 (Brasília)" in text


def test_a_target_is_shown_when_the_setup_has_one() -> None:
    assert "Alvo 1,1025" in format_message({**ARMED, "target": "1.10250"})


def test_a_closed_signal_says_its_result_in_r() -> None:
    won = format_message({**ARMED, "kind": "closed", "exit_price": "209800", "result_r": "5"})
    lost = format_message({**ARMED, "kind": "closed", "exit_price": "206500", "result_r": "-1"})

    assert won.startswith("✅ ENCERRADO #41")
    assert "Resultado: +5R" in won
    assert lost.startswith("❌ ENCERRADO #41")
    assert "Resultado: -1R" in lost


def test_a_cancelled_signal_says_why() -> None:
    text = format_message({**ARMED, "kind": "cancelled", "reason": "the setup withdrew it"})

    assert text.startswith("⚪ CANCELADO #41")
    assert "não foi executada" in text
    assert "Motivo: the setup withdrew it" in text


class Store:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and name in self.keys:
            return False
        self.keys.add(name)
        return True

    def delete(self, *names: str) -> int:
        self.keys -= set(names)
        return len(names)


class Network:
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self.name = name
        self.fails = fails
        self.posted: list[str] = []
        self.images: list[bytes | None] = []

    def post(self, text: str, image: bytes | None = None) -> None:
        if self.fails:
            raise RuntimeError("down")
        self.posted.append(text)
        self.images.append(image)


def test_a_network_that_already_has_an_entry_does_not_get_it_again() -> None:
    """A crash between posting and acknowledging re-reads the entry: Discord must not repeat it
    while Telegram, which failed, is tried again."""
    store, ok, down = Store(), Network("discord"), Network("telegram", fails=True)

    with pytest.raises(RuntimeError, match="telegram"):
        deliver("1-0", ARMED, [ok, down], store)
    down.fails = False
    deliver("1-0", ARMED, [ok, down], store)

    assert len(ok.posted) == 1
    assert len(down.posted) == 1


def test_a_rate_limit_is_waited_out_and_the_post_goes_through() -> None:
    calls: list[urllib.request.Request] = []
    waits: list[float] = []

    def fetch(request: urllib.request.Request) -> bytes:
        calls.append(request)
        if len(calls) == 1:
            body = io.BytesIO(json.dumps({"retry_after": 1.5}).encode())
            raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, body)  # type: ignore[arg-type]
        return b"{}"

    discord("https://discord.test/hook", fetch=fetch, sleep=waits.append).post("oi")

    assert waits == [1.5]
    assert len(calls) == 2
    assert json.loads(calls[1].data or b"") == {"content": "oi"}  # type: ignore[arg-type]


def test_telegram_goes_to_the_chat_with_the_disclaimer_in_plain_text() -> None:
    sent: list[urllib.request.Request] = []

    def fetch(request: urllib.request.Request) -> bytes:
        sent.append(request)
        return b"{}"

    telegram("TOKEN", "-100123", fetch=fetch).post(format_message(ARMED))

    body = json.loads(sent[0].data or b"")  # type: ignore[arg-type]
    assert sent[0].full_url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert body["chat_id"] == "-100123"
    assert body["text"].endswith(DISCLAIMER)
    assert not body["text"].endswith("_")


def test_a_refusal_says_the_networks_own_reason() -> None:
    def fetch(request: urllib.request.Request) -> bytes:
        body = io.BytesIO(b'{"description":"not enough rights to send text messages"}')
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match=r"telegram refused \(400\).*not enough rights"):
        telegram("TOKEN", "-1", fetch=fetch).post("oi")


BREAKEVEN = {
    **ARMED,
    "kind": "breakeven",
    "entry": "207055",
    "stop": "206500",
    "moved_stop": "207055.0",
    "target": "208165",
}


def test_a_breakeven_says_where_the_stop_went_and_that_the_trader_may_follow() -> None:
    text = format_message(BREAKEVEN)

    assert text.splitlines()[0] == "🔵 BREAKEVEN #41 — WIN H1"
    assert "Stop do setup movido para 207.055, na entrada (0x0)" in text
    assert "Compra executada em 207.055" in text
    assert "Alvo 208.165" in text
    assert "Se quiser, ajuste o stop da sua posição" in text


def test_a_breakeven_past_the_entry_says_so() -> None:
    text = format_message({**BREAKEVEN, "moved_stop": "207100", "target": ""})

    assert "Stop do setup movido para 207.100, além da entrada" in text
    assert "Alvo" not in text


def test_a_breakeven_with_a_price_it_cannot_read_is_not_called_zero_zero() -> None:
    text = format_message({**BREAKEVEN, "moved_stop": "?"})

    assert "além da entrada" in text


# --------------------------------------------------------------------------- outages


class Stream(Store):
    """`signals.events` as the notifier reads it: each read hands back the next scripted answer
    — a list of entries, or the error Redis would raise."""

    def __init__(self, reads: list[list[tuple[str, dict[str, str]]] | Exception]) -> None:
        super().__init__()
        self.reads = reads
        self.cursors: list[str] = []
        self.acked: list[str] = []
        self.groups: list[str] = []

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        self.cursors.append(streams[SIGNALS_STREAM])
        answer = self.reads.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return [(SIGNALS_STREAM, answer)] if answer else []

    def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.acked.extend(ids)
        return len(ids)

    def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "$",  # noqa: A002 — redis-py's own name for it, passed by keyword
        mkstream: bool = False,
    ) -> bool:
        self.groups.append(id)
        return True


class NoPictures:
    def get(self, name: str) -> bytes | None:
        return None


OUTAGE = logging.getLogger("tests.notifier.outage")
DOWN = RedisConnectionError("Error 10061 connecting to localhost:6379")


def serve(stream: Stream, network: Network, pending: str, waits: list[float]) -> str:
    return serve_once(
        stream,  # type: ignore[arg-type]
        NoPictures(),  # type: ignore[arg-type]
        [network],
        consumer="notifier-1",
        pending=pending,
        outage=Outage(OUTAGE),
        sleep=waits.append,
    )


def test_a_read_redis_does_not_answer_is_waited_out_and_nothing_is_lost() -> None:
    """12/10: Redis went away for minutes with the stack and the notifier died on its read, so
    the signals of the day that followed reached no channel until somebody restarted it."""
    stream, network = Stream([DOWN, DOWN, [("1-0", ARMED)]]), Network("discord")
    waits: list[float] = []

    pending = serve(stream, network, ">", waits)
    pending = serve(stream, network, pending, waits)
    pending = serve(stream, network, pending, waits)

    assert stream.cursors == [">", "0", "0"], "after a lost read, its own unacknowledged first"
    assert len(network.posted) == 1
    assert stream.acked == ["1-0"]
    assert waits == [OUTAGE_PAUSE, OUTAGE_PAUSE]


def test_a_group_redis_came_back_without_is_created_again_from_now() -> None:
    """From the start would post every signal still on the stream a second time."""
    stream = Stream([ResponseError("NOGROUP No such key 'signals.events'")])
    waits: list[float] = []

    assert serve(stream, Network("discord"), ">", waits) == "0"
    assert stream.groups == ["$"]


def test_any_other_refusal_from_redis_is_a_bug_and_is_not_waited_out() -> None:
    stream = Stream([ResponseError("WRONGTYPE Operation against a key of the wrong kind")])

    with pytest.raises(ResponseError, match="WRONGTYPE"):
        serve(stream, Network("discord"), ">", [])
