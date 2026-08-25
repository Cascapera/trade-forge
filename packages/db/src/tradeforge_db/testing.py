"""Emptying this schema between tests — which is not one statement any more.

Lives beside the models rather than in a `conftest.py` because **three test suites need it**
(`packages/db`, `apps/api`, `apps/collector`) and they cannot import each other's fixtures. Three
copies of a rule about how to empty a database is three chances to update two of them.

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

__all__ = ["truncate"]

_UNGUARD = "ALTER TABLE order_audit DISABLE TRIGGER USER"
_REGUARD = "ALTER TABLE order_audit ENABLE TRIGGER USER"


def truncate(connection: Connection, tables: Sequence[str]) -> None:
    """Empty `tables` and everything cascading from them, audit trail included.

    ⚠️ The re-enable is in a `finally`. A truncation that fails half way — a lock, a foreign key
    nobody expected — must not leave the database with its audit guard switched off, because the
    next thing to run would be a test suite that writes to it.
    """
    connection.execute(text(_UNGUARD))
    try:
        connection.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    finally:
        connection.execute(text(_REGUARD))
