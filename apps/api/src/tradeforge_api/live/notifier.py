"""`tradeforge-notifier`: posts each signal to Discord and Telegram (signals PR 6).

Reads `signals.events` (written by SIGNAL sessions, PR 5) as the consumer group `notifier`, and
posts **one new message per event** — never an edit (his call, 08/10) — carrying the signal's
number, so a reader finds "#41 armed" above "#41 triggered". Every message ends with the fixed
notice: "Conteúdo educacional. Não é recomendação de compra ou venda."

⚠️ **At least once, never twice.** An entry is acknowledged only after every configured network
took it; a crash in between re-reads it, and a key per (entry, network) — `signals:posted:…`,
kept a week — stops the network that already has it from getting it again.

Secrets come from `.env` or the environment only (`DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, read by `Settings`); a network whose settings are missing is skipped.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from redis import Redis
from redis.exceptions import ResponseError

from tradeforge_api.config import Settings
from tradeforge_api.live.signals import SIGNALS_STREAM

__all__ = [
    "DISCLAIMER",
    "Network",
    "deliver",
    "format_message",
    "main",
]

logger = logging.getLogger(__name__)

DISCLAIMER = "Conteúdo educacional. Não é recomendação de compra ou venda."
GROUP = "notifier"
_POSTED_TTL = 7 * 24 * 3600
_BRASILIA = ZoneInfo("America/Sao_Paulo")

_TITLE = {
    "armed": "🟡 ARMADO",
    "triggered": "🟢 ACIONADO",
    "cancelled": "⚪ CANCELADO",
}
_SIDE = {"long": "Compra", "short": "Venda"}
_ORDER = {"stop": "stop", "limit": "limite", "market": "a mercado"}


def _number(raw: str | None) -> str:
    """A price as the broker quotes it: no trailing zeros, thousands with a dot (pt-BR)."""
    if not raw:
        return "—"
    try:
        value = Decimal(raw).normalize()
    except InvalidOperation:
        return raw
    text = f"{value:,f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def _r(raw: str | None) -> str:
    if not raw:
        return "—"
    value = Decimal(raw).quantize(Decimal("0.01"))
    return f"{'+' if value > 0 else ''}{_number(str(value))}R"


def _when(raw: str | None) -> str:
    if not raw:
        return ""
    moment = dt.datetime.fromisoformat(raw).astimezone(_BRASILIA)
    return moment.strftime("%d/%m %H:%M") + " (Brasília)"


def format_message(fields: Mapping[str, str]) -> str:
    """The message for one event, in Portuguese, ending with the disclaimer."""
    kind = fields.get("kind", "")
    number = fields.get("number", "?")
    head = f"{fields.get('symbol', '?')} {fields.get('timeframe', '')}".strip()
    setup = fields.get("strategy", "")
    broker = fields.get("broker", "")
    side = _SIDE.get(fields.get("side", ""), fields.get("side", ""))
    order = _ORDER.get(fields.get("order_type", ""), fields.get("order_type", ""))

    if kind == "closed":
        result = fields.get("result_r", "")
        win = bool(result) and Decimal(result) > 0
        title = "✅ ENCERRADO" if win else "❌ ENCERRADO"
    else:
        title = _TITLE.get(kind, kind.upper())

    lines = [f"{title} #{number} — {head}"]
    lines.append(" · ".join(part for part in (setup, broker) if part))
    if kind == "armed":
        lines.append(f"{side} ({order}) em {_number(fields.get('entry'))}")
    elif kind == "triggered":
        lines.append(f"{side} executada em {_number(fields.get('entry'))}")
    elif kind == "cancelled":
        lines.append(f"{side} em {_number(fields.get('entry'))} não foi executada")
    if kind in {"armed", "triggered"}:
        lines.append(f"Stop {_number(fields.get('stop'))}")
        if fields.get("target"):
            lines.append(f"Alvo {_number(fields.get('target'))}")
        else:
            reach = _number(fields.get("no_target_r") or "5")
            lines.append(f"Sem alvo: encerra em {reach}R, no stop ou na saída do setup")
    if kind == "closed":
        lines.append(
            f"Entrada {_number(fields.get('entry'))} → saída {_number(fields.get('exit_price'))}"
        )
        lines.append(f"Resultado: {_r(fields.get('result_r'))}")
    if kind == "cancelled" and fields.get("reason"):
        lines.append(f"Motivo: {fields['reason']}")
    when = _when(fields.get("time"))
    if when:
        lines.append(when)
    lines.append("")
    lines.append(f"_{DISCLAIMER}_")
    return "\n".join(lines)


class Network(Protocol):
    """One place a message is posted to."""

    name: str

    def post(self, text: str) -> None:
        """Post it, or raise. A rate limit is waited out inside."""
        ...


Fetch = Callable[[urllib.request.Request], bytes]

# ⚠️ A User-Agent of our own (08/10): Discord's edge refuses Python's default `Python-urllib/3.x`
# with a 403, while the same request from curl is taken — measured on the first test post.
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "TradeForge-Signals (https://github.com/Cascapera/trade-forge, 1.0)",
}


def _fetch(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 — https only
        return bytes(response.read())


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    if error.code != 429:  # noqa: PLR2004 — HTTP's "too many requests"
        return None
    try:
        body = json.loads(error.read() or b"{}")
    except ValueError:
        body = {}
    seconds = body.get("retry_after") or body.get("parameters", {}).get("retry_after") or 5
    return float(seconds)


@dataclass(slots=True)
class _Http:
    name: str
    url: str
    payload: Callable[[str], dict[str, object]]
    fetch: Fetch = _fetch
    sleep: Callable[[float], None] = time.sleep

    def post(self, text: str) -> None:
        body = json.dumps(self.payload(text)).encode()
        for _attempt in range(5):
            request = urllib.request.Request(  # noqa: S310 — the URL is ours, https
                self.url, data=body, headers=_HEADERS, method="POST"
            )
            try:
                self.fetch(request)
            except urllib.error.HTTPError as error:
                wait = _retry_after(error)
                if wait is None:
                    # The network's own sentence, not only its code: "not enough rights to send
                    # text messages to the chat" is what told us the bot needed posting rights.
                    reason = error.read().decode(errors="replace")[:300]
                    raise RuntimeError(f"{self.name} refused ({error.code}): {reason}") from error
                logger.warning("%s rate limit; waiting %.1fs", self.name, wait)
                self.sleep(wait)
            else:
                return
        raise RuntimeError(f"{self.name} kept refusing: rate limited five times")


def discord(webhook_url: str, **seams: object) -> Network:
    """A Discord channel's webhook."""
    return _Http(
        "discord",
        webhook_url,
        lambda text: {"content": text},  # `_..._` is italic in Discord's markdown
        **seams,  # type: ignore[arg-type]
    )


def telegram(token: str, chat_id: str, **seams: object) -> Network:
    """A Telegram chat or channel, through the bot."""
    return _Http(
        "telegram",
        f"https://api.telegram.org/bot{token}/sendMessage",
        lambda text: {
            "chat_id": chat_id,
            "text": text.replace("_" + DISCLAIMER + "_", DISCLAIMER),
            "disable_web_page_preview": True,
        },
        **seams,  # type: ignore[arg-type]
    )


class _Store(Protocol):
    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> object: ...

    def delete(self, *names: str) -> object: ...


def deliver(
    entry_id: str, fields: Mapping[str, str], networks: Sequence[Network], store: _Store
) -> None:
    """Post one entry to every network that does not have it yet. Raises if any refused — the
    caller then leaves the entry unacknowledged, and the next read tries the rest again."""
    text = format_message(fields)
    failed: list[str] = []
    for network in networks:
        key = f"signals:posted:{entry_id}:{network.name}"
        if not store.set(key, "1", ex=_POSTED_TTL, nx=True):
            continue  # this network already has it
        try:
            network.post(text)
        except Exception:
            store.delete(key)
            logger.exception("posting %s to %s failed", entry_id, network.name)
            failed.append(network.name)
    if failed:
        raise RuntimeError(f"not delivered to {', '.join(failed)}")


def _networks(settings: Settings) -> list[Network]:
    networks: list[Network] = []
    webhook = settings.discord_webhook_url.get_secret_value()
    if webhook:
        networks.append(discord(webhook))
    else:
        logger.warning("DISCORD_WEBHOOK_URL is not set: nothing goes to Discord")
    token, chat = settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
    if token and chat:
        networks.append(telegram(token, chat))
    else:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not both set: nothing goes to Telegram")
    return networks


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-notifier", description="Post each signal to Discord and Telegram."
    )
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="read the stream from its first entry (default: only events from now on)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="post one test message to every configured network and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = Settings()
    networks = _networks(settings)
    if not networks:
        logger.error("no network configured; set DISCORD_WEBHOOK_URL and/or TELEGRAM_*")
        return 2
    if args.test:
        for network in networks:
            network.post(f"🔧 Teste do TradeForge: o canal de sinais está ligado.\n\n{DISCLAIMER}")
            logger.info("test posted to %s", network.name)
        return 0

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        redis.xgroup_create(
            SIGNALS_STREAM, GROUP, id="0" if args.from_start else "$", mkstream=True
        )
    except ResponseError as exists:
        if "BUSYGROUP" not in str(exists):
            raise
    consumer = f"notifier-{os.getpid()}"
    logger.info("posting signals to %s", ", ".join(network.name for network in networks))
    pending = "0"  # first our own unacknowledged entries, then new ones
    try:
        while True:
            read = cast(
                "list[tuple[str, list[tuple[str, dict[str, str]]]]]",
                redis.xreadgroup(GROUP, consumer, {SIGNALS_STREAM: pending}, count=20, block=5000),
            )
            entries = read[0][1] if read else []
            if pending == "0" and not entries:
                pending = ">"
                continue
            for entry_id, fields in entries:
                try:
                    deliver(entry_id, fields, networks, redis)
                except Exception:  # noqa: BLE001 - any failure keeps the entry for another try
                    logger.warning("entry %s kept for another try", entry_id)
                    time.sleep(10)
                    pending = "0"
                    break
                redis.xack(SIGNALS_STREAM, GROUP, entry_id)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        redis.close()
    return 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
