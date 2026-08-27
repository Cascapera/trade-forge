"""The three things a *process* has that a function does not: argv, signals, an exit code.

Everything that decides anything is in `session.py` and is tested against a database. This file
is only the shell, and the part of it worth pinning is the signal handling — which is easy to
get wrong in ways that never show up until a shutdown goes badly on a real machine.
"""

import inspect
import signal
import threading

import pytest

from tradeforge_api.live.process import _parse, main, stop_on_signals
from tradeforge_db.models import SessionMode


@pytest.fixture(autouse=True)
def restore_handlers() -> object:
    """⚠️ Signal handlers are process-global. A test that installs one and does not put the old
    one back changes how *pytest itself* reacts to Ctrl-C for the rest of the run."""
    saved = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    yield None
    for number, handler in saved.items():
        signal.signal(number, handler)


def test_a_signal_asks_the_session_to_stop() -> None:
    """The whole contract: the process is asked to stop, and the flag the session reads goes up.

    `raise_signal` delivers a real signal to this process, so this exercises the handler Python
    actually installed rather than calling the function directly.
    """
    stopping = threading.Event()
    stop_on_signals(stopping)

    assert not stopping.is_set()
    signal.raise_signal(signal.SIGINT)

    assert stopping.is_set(), "the session was never told to stop"


def test_the_handler_only_sets_a_flag() -> None:
    """⚠️ It must not finish the session, roll back or close anything. A Python signal handler
    runs on the main thread *between bytecodes*, so real work there happens in the middle of
    whatever the session was doing — including halfway through a commit.

    Separated by checking that the call returns at once and changes nothing but the flag: a
    handler that did the shutdown itself would have to touch a database this test never gave it.
    """
    stopping = threading.Event()
    stop_on_signals(stopping)

    signal.raise_signal(signal.SIGTERM)

    assert stopping.is_set()


def test_a_second_signal_is_left_to_the_default_handler() -> None:
    """⚠️ Somebody sending it twice is saying the orderly stop is taking too long, and the honest
    answer is to die — leaving a `running` row that `reconcile_stale` will settle, rather than a
    process that ignores its operator.

    Checked on the *installed handler*, because the observable consequence — the process being
    killed — is not something a test can survive.
    """
    stopping = threading.Event()
    stop_on_signals(stopping)
    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN)

    signal.raise_signal(signal.SIGTERM)

    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, (
        "a second SIGTERM would still be swallowed"
    )


def test_both_stop_signals_are_handled() -> None:
    """`SIGTERM` is what `docker stop` and systemd send; `SIGINT` is Ctrl-C. Handling one and
    not the other means a session that stops cleanly in a terminal and is killed in production,
    or the reverse — and the difference only shows up on the machine that matters."""
    stopping = threading.Event()
    stop_on_signals(stopping)

    for number in (signal.SIGINT, signal.SIGTERM):
        assert signal.getsignal(number) not in (signal.SIG_DFL, signal.SIG_IGN), (
            f"{signal.Signals(number).name} was left unhandled"
        )


def test_the_arguments_a_session_needs_are_all_required() -> None:
    """A session with a missing instrument or capital is not a session with a default; it is a
    mistake, and `argparse` should say so rather than start something."""
    for missing in ("--strategy", "--instrument", "--timeframe", "--capital"):
        argv = [
            "--strategy",
            "11111111-1111-1111-1111-111111111111",
            "--instrument",
            "22222222-2222-2222-2222-222222222222",
            "--timeframe",
            "H1",
            "--capital",
            "10000",
        ]
        index = argv.index(missing)
        del argv[index : index + 2]

        with pytest.raises(SystemExit):
            _parse(argv)


def test_the_capital_is_parsed_as_a_decimal_not_a_float() -> None:
    """⚠️ `float("0.1")` is not 0.1, and initial capital is the denominator every percent-risk
    size is computed from. The whole engine runs in exact decimal; the boundary has to hand it
    one."""
    args = _parse(
        [
            "--strategy",
            "11111111-1111-1111-1111-111111111111",
            "--instrument",
            "22222222-2222-2222-2222-222222222222",
            "--timeframe",
            "H1",
            "--capital",
            "10000.10",
        ]
    )

    assert str(args.capital) == "10000.10", "the capital went through a float"


def test_no_spread_means_no_cost_model_rather_than_a_zero_one() -> None:
    """A cost model of zero and no cost model are the same number and different statements, and
    `--spread-points` omitted is the second. Defaulting it to zero would have a paper session
    quietly claim it had measured costs and found none."""
    args = _parse(
        [
            "--strategy",
            "11111111-1111-1111-1111-111111111111",
            "--instrument",
            "22222222-2222-2222-2222-222222222222",
            "--timeframe",
            "H1",
            "--capital",
            "10000",
        ]
    )

    assert args.spread_points is None


def a_command(*extra: str) -> list[str]:
    """The four arguments every session needs, plus whatever a test is actually about."""
    return [
        "--strategy",
        "11111111-1111-1111-1111-111111111111",
        "--instrument",
        "22222222-2222-2222-2222-222222222222",
        "--timeframe",
        "H1",
        "--capital",
        "10000",
        *extra,
    ]


def test_a_session_is_paper_unless_the_operator_says_otherwise() -> None:
    """⚠️ **The third place this default has to hold**, after `SessionPlan.mode` and
    `open_session`. It is also the one that matters most, because it is the one a human types:
    the safe value costs nothing and the dangerous one costs a flag, in as many words.
    """
    assert _parse(a_command()).mode is SessionMode.PAPER


def test_live_is_reachable_and_only_by_naming_it() -> None:
    """Stated and it happens; mistyped and it is a parse error rather than a mode.

    ⚠️ `choices` rather than a `--live` switch, so that "this is a paper session" is also
    something an operator can say out loud — a flag whose absence is the whole statement reads
    the same whether it was considered or forgotten.
    """
    assert _parse(a_command("--mode", "live")).mode is SessionMode.LIVE
    assert _parse(a_command("--mode", "paper")).mode is SessionMode.PAPER

    with pytest.raises(SystemExit):
        _parse(a_command("--mode", "LIVE"))
    with pytest.raises(SystemExit):
        _parse(a_command("--mode", "real"))


def test_the_promotion_gate_reads_its_number_from_configuration() -> None:
    """⚠️ `LIVE_PROMOTION_DAYS` used to move a number nothing consulted: `run_session` fell
    through to its own default of five, which happened to agree. A configuration option nobody
    reads is worse than none — it is a promise on a page — so this pins that `main` passes it.
    """
    source = inspect.getsource(main)
    assert "promotion_days=settings.live_promotion_days" in source, (
        "main stopped passing the configured number; the gate is back to a hard-coded 5"
    )
