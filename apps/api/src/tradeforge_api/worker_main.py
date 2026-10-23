"""`python -m tradeforge_api.worker_main` starts a backtest worker that claims in one round trip.

The same worker as `arq tradeforge_api.worker.WorkerSettings` — same settings, same functions,
same log — built on `OneTripWorker` (`claim`) instead of arq's own class, which its command line
has no way to swap. The old command still works: it is only slower to find a job from far away.
"""

from __future__ import annotations

import logging.config

from arq.logs import default_log_config
from arq.worker import get_kwargs

from tradeforge_api.claim import OneTripWorker
from tradeforge_api.worker import WorkerSettings


def main() -> None:
    logging.config.dictConfig(default_log_config(verbose=False))
    OneTripWorker(**get_kwargs(WorkerSettings)).run()  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
