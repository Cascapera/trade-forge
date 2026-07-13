"""`tradeforge-health` — is the stack actually up?

Exits non-zero when any service is unhealthy, so it composes with a shell and can
gate a startup script.
"""

import sys

from tradeforge_api.config import Settings
from tradeforge_api.health import ServiceHealth, check_postgres, check_redis


def _printable(text: str, encoding: str | None) -> str:
    """Force `text` through the console's encoding, replacing what will not fit.

    A driver's error message can carry any character the operating system feels
    like using — an accented Windows locale, say — while a Windows console still
    speaks cp1252. Left alone, `print` raises UnicodeEncodeError and the tool
    whose entire job is to report the failure dies while reporting it.
    """
    codec = encoding or "utf-8"
    return text.encode(codec, errors="replace").decode(codec, errors="replace")


def _report(services: list[ServiceHealth]) -> None:
    for service in services:
        mark = "OK  " if service.ok else "FAIL"
        line = f"[{mark}] {service.name:<10} {service.detail}"
        print(_printable(line, sys.stdout.encoding))


def main() -> int:
    """Check every dependency and return a shell exit code."""
    settings = Settings()

    services = [
        check_postgres(settings.postgres_dsn),
        check_redis(settings.redis_url),
    ]
    _report(services)

    return 0 if all(service.ok for service in services) else 1
