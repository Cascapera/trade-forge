"""The host agent, checked on Linux CI — which is the whole trick.

This module is the only place in the system that may talk to MetaTrader from a job, and it runs
on a machine that has MetaTrader. None of that stops it being tested here: the import of
`MetaTrader5` lives *inside* the job body, so the module imports fine on a box where the library
cannot even be installed, and everything around the terminal call is ordinary code.
"""

import datetime as dt

import pytest
from arq.connections import RedisSettings

from tradeforge_collector import agent
from tradeforge_collector.source import SymbolInfo


def test_the_module_imports_without_metatrader() -> None:
    """⚠️ The ADR-02 property, asserted rather than assumed.

    If the MetaTrader import ever drifts to module scope, this file stops collecting on Linux —
    and so does `tests/test_queue_contract.py`, which is the only thing checking that the API
    and this agent agree on a job name. The failure would be a red CI, which is the good
    outcome; the point of the test is that it is the *first* thing to go red rather than a
    mystery further down.
    """
    assert agent.WorkerSettings.functions


def test_a_symbol_crosses_from_the_adapter_to_the_catalogue_whole() -> None:
    entry = agent._entry(
        SymbolInfo(
            symbol="EURUSD",
            description="Euro vs US Dollar",
            path="Forex\\Majors\\EURUSD",
            digits=5,
            visible=True,
        )
    )

    assert entry.symbol == "EURUSD"
    assert entry.description == "Euro vs US Dollar"
    assert entry.path == "Forex\\Majors\\EURUSD"
    assert entry.digits == 5
    assert entry.visible is True


def test_a_symbol_the_broker_described_with_nothing_keeps_its_nulls() -> None:
    """⚠️ `None`, not `""`, and the mapping must not quietly invent the difference.

    MetaTrader returns the empty string for a field it was never given, and the adapter already
    converts that to `None` (see `MT5Source.symbols`). If this mapping re-flattened it, the
    screen could not tell "this broker gives no description" from "the description is blank",
    and would render an empty line either way with no idea which it was showing.
    """
    entry = agent._entry(SymbolInfo(symbol="XAUUSD"))

    assert entry.description is None
    assert entry.path is None
    assert entry.digits is None
    assert entry.visible is False


class TestTheStatedOffset:
    def test_an_absent_variable_means_measure_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`None` is what asks the source to measure. An accidental zero would claim UTC."""
        monkeypatch.delenv("TRADEFORGE_SERVER_OFFSET", raising=False)

        assert agent._stated_offset() is None

    @pytest.mark.parametrize(("given", "hours"), [("+3", 3.0), ("3", 3.0), ("-5.5", -5.5)])
    def test_hours_ahead_of_utc_are_read(
        self, monkeypatch: pytest.MonkeyPatch, given: str, hours: float
    ) -> None:
        monkeypatch.setenv("TRADEFORGE_SERVER_OFFSET", given)

        assert agent._stated_offset() == dt.timedelta(hours=hours)

    def test_zero_is_a_stated_offset_and_not_an_absent_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ The case a truthiness check gets wrong.

        A broker really can run on UTC, and `timedelta()` is falsy. Collapsing it to "not
        stated" would send the agent back to measuring the clock from the newest tick — which
        is refused while the market is shut, so the agent would fail to connect all weekend for
        an operator who had configured it correctly.
        """
        monkeypatch.setenv("TRADEFORGE_SERVER_OFFSET", "0")

        assert agent._stated_offset() == dt.timedelta()

    def test_a_malformed_offset_is_refused_rather_than_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Falling back to `None` would turn a typo into "measure it", and the difference only
        # shows up as candles stamped hours out — silently, which is this project's worst case.
        monkeypatch.setenv("TRADEFORGE_SERVER_OFFSET", "three")

        with pytest.raises(ValueError, match="hours ahead of UTC"):
            agent._stated_offset()


class TestTheWorkerSettings:
    def test_it_drains_its_own_queue(self) -> None:
        assert agent.WorkerSettings.queue_name == agent.COLLECT_QUEUE

    def test_it_runs_exactly_one_job_at_a_time(self) -> None:
        """⚠️ **This is the whole answer to "do not overload the machine".**

        A batch collection queues one job per symbol (PR-236), so twenty symbols is twenty jobs
        sitting on this queue at once. Nothing throttles them anywhere else, and nothing needs
        to: this number is what makes twenty jobs run one after another instead of together.

        Raising it would not make the work faster. The terminal is a single shared IPC channel
        and the measured cost of a cold history request is the terminal *downloading* — two
        jobs asking at once contend for the same download. It would, however, multiply the peak
        memory by the number of concurrent jobs, since each one holds a calendar year of bars
        (368 000 of them at M1), and let a sync observe a half-written snapshot.

        Asserted rather than trusted to the comment beside it, because a comment does not fail.
        """
        assert agent.WorkerSettings.max_jobs == 1

    def test_redis_is_a_value_and_not_something_to_call(self) -> None:
        """⚠️ **The bug this test exists for, found by starting the worker for real.**

        arq *reads* `WorkerSettings.redis_settings`; it never calls it. The first version here
        was a `@staticmethod`, and the tests below — which call it — passed happily while the
        worker died on startup with `'staticmethod' object has no attribute 'host'`. A test
        that invokes the thing arq only ever reads is a test agreeing with itself.
        """
        assert isinstance(agent.WorkerSettings.redis_settings, RedisSettings)
        assert not callable(agent.WorkerSettings.redis_settings)

    def test_redis_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ The agent lives outside the compose file, so it cannot inherit its networking.

        Containers reach Redis at the service name `redis`; this process is on the host, where
        the same server is `localhost` on the published port. Hard-coding either one breaks the
        other, and the failure is a worker that starts cleanly and drains a queue nobody writes.
        """
        monkeypatch.setenv("REDIS_HOST", "somewhere-else")
        monkeypatch.setenv("REDIS_PORT", "6380")

        settings = agent.redis_settings_from_env()

        assert settings.host == "somewhere-else"
        assert settings.port == 6380

    def test_the_defaults_are_the_host_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDIS_HOST", raising=False)
        monkeypatch.delenv("REDIS_PORT", raising=False)

        settings = agent.redis_settings_from_env()

        assert (settings.host, settings.port) == ("localhost", 6379)
