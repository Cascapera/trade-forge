"""Emptying this schema between tests — which is not one statement any more.

Lives beside the models rather than in a `conftest.py` because **three test suites need it**
(`packages/db`, `apps/api`, `apps/executor`) and they cannot import each other's fixtures. Three
copies of a rule about how to empty a database is three chances to update two of them — and it is
also why the refusal to empty a database that is not disposable belongs in here. See `truncate`.

⚠️ **`order_audit` is append-only and a trigger enforces it** (rev_0015) — against `TRUNCATE` as
well as `UPDATE` and `DELETE`, because `TRUNCATE` is not a `DELETE` as far as Postgres is
concerned and a row-level guard alone would let one statement empty the table.

That collides with wanting a clean database per test, and the collision is wider than it looks.
Measured on a scratch database, even

    TRUNCATE datasets, instruments RESTART IDENTITY CASCADE

— which names neither `live_sessions` nor `order_audit` — reports
`NOTICE: truncate cascades to table "order_audit"` and then aborts the **whole statement**. Any
truncation that reaches `instruments` reaches the audit trail, and with a single audit row present
every integration test in the project fails in its fixture.

So the guard is lifted for exactly the length of the truncation and put straight back.

**Why not simply leave the trigger out of test databases.** Because a guard that does not exist
where the tests run is a guard no test can prove, and this one is the difference between an audit
trail and a log. `test_order_audit_integration.py` proves it by attempting all three statements
against the same schema production gets.
"""

from collections.abc import Sequence

from sqlalchemy import Connection, text

__all__ = ["TEST_DATABASE_SUFFIX", "NotDisposableError", "truncate"]

_UNGUARD = "ALTER TABLE order_audit DISABLE TRIGGER USER"
_REGUARD = "ALTER TABLE order_audit ENABLE TRIGGER USER"

TEST_DATABASE_SUFFIX = "_test"
"""What a database's name must end in before anything here is allowed to empty it.

⚠️ **A suffix on the target, deliberately, and not a flag on the caller.** An environment
variable that says "yes, destroy things" is set once in a shell and stays set for every command
typed afterwards, including the one nobody meant to point at a real database. A name is a
property of the thing being destroyed: it travels with the target, cannot be left switched on,
and reads at the call site as what it is.

**An allowlist, not a blocklist.** Refusing a list of precious names fails *open* — the database
somebody creates next is unprotected and nothing says so. Requiring the suffix fails closed:
anything unrecognised is treated as precious, which is the safe direction for a statement whose
mistake is unrecoverable.
"""


class NotDisposableError(RuntimeError):
    """Raised when something asks to empty a database that is not named as disposable."""


def truncate(connection: Connection, tables: Sequence[str]) -> None:
    """Empty `tables` and everything cascading from them, audit trail included.

    ⚠️ **Refuses unless the database it is connected to is named `*_test`, and this is the whole
    reason that check lives here rather than in a fixture.** Measured on 2026-08-31: the
    `tradeforge` database held **0 trades and 0 backtests**, against 24 001 trades recorded three
    days earlier — an integration run had pointed at it, emptied six tables and left its own
    fixtures behind. That was the **second** time (the first took his backtests on 2026-08-04).

    Three test suites already carried the warning in prose — *"the variable is not optional;
    without it the integration suite truncates whatever database the environment points at"* —
    and prose is what failed. A rule that only exists in a docstring makes the next reader stop
    looking, which is exactly what a rule is supposed to prevent.

    So it is enforced by the function that does the destroying. A guard at the call site is a
    guard the fourth `conftest.py` can forget to copy; there is no path to the `TRUNCATE` that
    does not come through this line.

    ⚠️ **Asked of the server, not of the URL.** `current_database()` is what the statement will
    actually act on; an engine URL is what somebody believes it will act on, and the whole
    failure mode here is the gap between those two.

    ⚠️ The re-enable is in a `finally`. A truncation that fails half way — a lock, a foreign key
    nobody expected — must not leave the database with its audit guard switched off, because the
    next thing to run would be a test suite that writes to it.
    """
    # ⚠️ **Before `_UNGUARD`, and that ordering is load-bearing.** The unguard disables the
    # trigger that makes `order_audit` append-only. A refusal raised after it would leave a real
    # database with its audit guard switched off, which is a worse state than the one this
    # function was refusing to create.
    database = connection.execute(text("SELECT current_database()")).scalar_one()
    if not database.endswith(TEST_DATABASE_SUFFIX):
        raise NotDisposableError(
            f"refusing to empty {database!r}: this truncates six tables and cannot be undone, "
            f"and only a database named '*{TEST_DATABASE_SUFFIX}' is treated as disposable. "
            f"Run the integration suite with POSTGRES_DB={database}{TEST_DATABASE_SUFFIX}."
        )
    connection.execute(text(_UNGUARD))
    try:
        connection.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    finally:
        connection.execute(text(_REGUARD))
