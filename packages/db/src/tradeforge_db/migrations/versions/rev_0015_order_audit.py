"""every order the executor was asked to send, in a table nobody can tidy up

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-25

The table `sdd.md` §5 and §11 have been promising since the design: an append-only record of
every order requested, sent, executed or rejected, with timestamps.

**Append-only is enforced here, by a trigger, not by discipline in the application.** That is the
whole difference between an audit trail and a log. A table that *could* be updated is a table
somebody can quietly correct after a bad day — including, especially, the person with the most
reason to want to. The value of this record is precisely that it survives its author's
embarrassment, and a rule that lives only in the code that writes it does not.

⚠️ The trigger refuses `UPDATE` and `DELETE` for **everyone**, superuser included, and that is
deliberate. A guard with an exemption is a guard whose exemption becomes the normal path. Undoing
it is a migration: visible, reviewed, dated — the same doctrine `rev_0012` used to gate
`mode='live'` behind a CHECK rather than behind a configuration value.

⚠️ **`TRUNCATE` is refused too, and separately.** It is not a `DELETE` as far as Postgres is
concerned — a `FOR EACH ROW` trigger never sees it — so a table protected only against `DELETE`
can still be emptied in one statement. This project already learned that the hard way from the
other side: the integration suite truncates eleven tables, and `order_audit` is the one table
that must survive a test run pointed at the wrong database.

**No `requested` status**, though the sketch in §5 had one. A row saying "I picked this up" would
have to be updated when the outcome arrived, and updates are exactly what this table forbids. So
a row is written once, when its outcome is known, carrying both instants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("sent", "filled", "partial", "refused", "error")

# ⚠️ `RAISE EXCEPTION`, not `RETURN NULL`. A `BEFORE` trigger that returns NULL silently drops
# the statement: the UPDATE reports success, changes nothing, and the caller believes it worked.
# An audit trail that lies about being immutable is worse than one that is not.
_GUARD = """
CREATE OR REPLACE FUNCTION order_audit_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'order_audit is append-only; % is refused (see rev_0015)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

_ROW_GUARD = """
CREATE TRIGGER order_audit_no_update_or_delete
BEFORE UPDATE OR DELETE ON order_audit
FOR EACH ROW EXECUTE FUNCTION order_audit_is_append_only();
"""

# Separate, and `FOR EACH STATEMENT`, because TRUNCATE has no rows to iterate. A table guarded
# only per-row can still be emptied in a single statement.
_TRUNCATE_GUARD = """
CREATE TRIGGER order_audit_no_truncate
BEFORE TRUNCATE ON order_audit
FOR EACH STATEMENT EXECUTE FUNCTION order_audit_is_append_only();
"""


def upgrade() -> None:
    op.create_table(
        "order_audit",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("live_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["live_session_id"],
            ["live_sessions.id"],
            name="fk_order_audit_live_session_id_live_sessions",
            # ⚠️ RESTRICT. Deleting a session must not take its audit trail with it — that is
            # the one deletion an audit trail exists to survive.
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN " + str(_STATUSES),
            # ⚠️ Bare. The naming convention prefixes `ck_order_audit_`, and spelling the prefix
            # here produces `ck_order_audit_ck_order_audit_...` — the duplication PR #139 had to
            # go and undo across 31 constraints.
            name="order_audit_status",
        ),
        sa.CheckConstraint(
            "(reason IS NULL) <> (status IN ('refused', 'error'))",
            name="a_refusal_says_why",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= requested_at",
            name="audit_resolves_after_it_is_requested",
        ),
    )
    op.create_index("ix_order_audit_live_session_id", "order_audit", ["live_session_id"])
    op.create_index("ix_order_audit_client_id", "order_audit", ["client_id"])
    op.create_index("ix_order_audit_requested_at", "order_audit", ["requested_at"])

    op.execute(_GUARD)
    op.execute(_ROW_GUARD)
    op.execute(_TRUNCATE_GUARD)


def downgrade() -> None:
    # ⚠️ The triggers go first. `DROP TABLE` would take them with it, but naming them here keeps
    # the downgrade readable as the exact inverse — and a future revision that drops only the
    # guard has this line to copy.
    op.execute("DROP TRIGGER IF EXISTS order_audit_no_truncate ON order_audit")
    op.execute("DROP TRIGGER IF EXISTS order_audit_no_update_or_delete ON order_audit")
    op.drop_index("ix_order_audit_requested_at", table_name="order_audit")
    op.drop_index("ix_order_audit_client_id", table_name="order_audit")
    op.drop_index("ix_order_audit_live_session_id", table_name="order_audit")
    op.drop_table("order_audit")
    op.execute("DROP FUNCTION IF EXISTS order_audit_is_append_only()")
