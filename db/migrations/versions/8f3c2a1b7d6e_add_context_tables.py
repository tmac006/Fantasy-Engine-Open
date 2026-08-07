"""add source-aware context tables

Revision ID: 8f3c2a1b7d6e
Revises: 42e19203c52e
Create Date: 2026-08-04 13:05:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8f3c2a1b7d6e"
down_revision: str | Sequence[str] | None = "42e19203c52e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "player_context",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("canonical_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["canonical_id"], ["player_ids.canonical_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_id", "season", "source", name="uq_player_context_key"
        ),
    )
    op.create_index(
        op.f("ix_player_context_canonical_id"),
        "player_context",
        ["canonical_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_player_context_season"),
        "player_context",
        ["season"],
        unique=False,
    )

    op.create_table(
        "team_context",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("team", sa.String(length=8), nullable=False),
        sa.Column("season", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team", "season", "source", name="uq_team_context_key"),
    )
    op.create_index(
        op.f("ix_team_context_season"),
        "team_context",
        ["season"],
        unique=False,
    )
    op.create_index(
        op.f("ix_team_context_team"),
        "team_context",
        ["team"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_team_context_team"), table_name="team_context")
    op.drop_index(op.f("ix_team_context_season"), table_name="team_context")
    op.drop_table("team_context")
    op.drop_index(
        op.f("ix_player_context_season"), table_name="player_context"
    )
    op.drop_index(
        op.f("ix_player_context_canonical_id"), table_name="player_context"
    )
    op.drop_table("player_context")
