"""a sweep's points are rows of their own

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-26

His ask (26/09): a grid of 435 thousand points per chart must launch without loading it whole.
`sweeps.points` was one JSONB list per sweep — past Postgres's 255 MB ceiling for a sweep over
every chart, and read whole by every reader. Each point becomes a row of `sweep_points`, written
in blocks by the launch; the existing lists are moved over in order and the column dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sweep_points",
        sa.Column(
            "sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("coordinates", JSONB(), nullable=False),
        sa.Column("same_as", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "coordinates_are_an_object", "sweep_points", "jsonb_typeof(coordinates) = 'object'"
    )
    op.create_index(
        "ix_sweep_points_sweep_id_strategy_id", "sweep_points", ["sweep_id", "strategy_id"]
    )
    # In the order each list held them, which is the order its launch wrote them in.
    op.execute(
        """
        INSERT INTO sweep_points
            (sweep_id, position, strategy_id, entry_id, label, coordinates, same_as)
        SELECT s.id, p.ordinality - 1, (p.point ->> 'strategy_id')::uuid, p.point ->> 'entry_id',
               p.point ->> 'label', p.point -> 'values', p.point ->> 'same_as'
        FROM sweeps s, jsonb_array_elements(s.points) WITH ORDINALITY AS p(point, ordinality)
        """
    )
    op.drop_constraint("points_are_a_list", "sweeps", type_="check")
    op.drop_column("sweeps", "points")


def downgrade() -> None:
    op.add_column("sweeps", sa.Column("points", JSONB(), nullable=False, server_default="[]"))
    op.create_check_constraint("points_are_a_list", "sweeps", "jsonb_typeof(points) = 'array'")
    op.execute(
        """
        UPDATE sweeps s SET points = p.points
        FROM (
            SELECT sweep_id, jsonb_agg(
                jsonb_strip_nulls(jsonb_build_object(
                    'strategy_id', strategy_id::text, 'entry_id', entry_id, 'label', label,
                    'same_as', same_as
                )) || jsonb_build_object('values', coordinates)
                ORDER BY position
            ) AS points
            FROM sweep_points GROUP BY sweep_id
        ) p
        WHERE p.sweep_id = s.id
        """
    )
    op.drop_table("sweep_points")
