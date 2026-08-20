"""The queue is an interface between two apps, and it is spelled out in both of them.

`apps/api` enqueues a job by **name**, on a queue by **name**. `apps/collector` registers a
coroutine under that name, on a queue with that name. Neither imports the other — deliberately,
because one of them runs in a Linux container and the other only exists on a Windows host — so
nothing in either app's own test suite can notice the day the two spellings drift apart.

What that failure looks like is the reason this file exists: a typo produces no error anywhere.
The API accepts the request and returns `202`, Redis accepts the job, and it sits in a queue no
worker is watching, for ever. The screen shows "syncing…" and nothing ever happens.

⚠️ Lives here, beside `test_architecture.py`, rather than in either app. It is an invariant of
the *repository* — the same category as "only the collector imports MetaTrader5" — and putting
it inside one app would make that app depend on the other to run its own tests.

Importing `tradeforge_collector.agent` from Linux CI is safe by construction: the MetaTrader
import lives inside the job body, which is the same ADR-02 trick that lets the whole collector
package be importable on a machine where the library cannot even be installed.
"""

from tradeforge_api import queue as api_queue
from tradeforge_collector import agent as host_agent


def test_the_queue_the_api_writes_to_is_the_one_the_agent_drains() -> None:
    """Two constants, two apps, one Redis key. They have to be the same string."""
    assert api_queue.COLLECT_QUEUE == host_agent.COLLECT_QUEUE
    assert host_agent.WorkerSettings.queue_name == host_agent.COLLECT_QUEUE


def test_the_two_apps_agree_on_exactly_which_jobs_the_host_runs() -> None:
    """⚠️ The typo this whole file exists for, checked in both directions.

    arq dispatches by the coroutine's `__name__`, so the name in `queue.py` and the name of the
    function in `agent.py` are the contract. Mistype either and the job is accepted, queued, and
    never claimed — no exception, no log, no timeout. The request even succeeds.

    Compared as **sets**, not with an `in`. An `in` check passes forever while somebody adds a
    second job to one side and forgets the other, which is precisely what happened between
    `sync_symbols` and `probe_history` — the test stayed green through the addition and proved
    nothing about it.
    """
    registered = {function.__name__ for function in host_agent.WorkerSettings.functions}

    assert set(api_queue.COLLECT_JOBS) == registered


def test_the_host_agent_does_not_also_claim_the_backtest_jobs() -> None:
    """The segregation has to run both ways to be worth anything.

    A worker that drained the default queue as well would pull a walk-forward onto the Windows
    box — where there is one job slot, because the terminal is a single shared resource — and
    the API's own worker would sit idle while a twenty-minute backtest blocked every symbol
    sync behind it. That is the exact latency problem the second queue was created to avoid,
    reintroduced from the other side.
    """
    registered = {function.__name__ for function in host_agent.WorkerSettings.functions}

    assert api_queue.RUN_BACKTEST not in registered
    assert api_queue.RUN_WALK_FORWARD not in registered


def test_the_agent_runs_one_job_at_a_time() -> None:
    """The terminal is one process with one IPC channel.

    Measured on this project's broker: the cost of a cold history request is the terminal
    *downloading* it — up to 207 s — and two jobs asking at once contend for that download
    rather than sharing it. Serial is also what makes a snapshot replacement unobservable
    half-written by a second job.
    """
    assert host_agent.WorkerSettings.max_jobs == 1
