"""add injury statuses to league rosters

Revision ID: b4e1c7d2a9f3
Revises: 9d2c7fae70a1
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4e1c7d2a9f3'
down_revision: Union[str, Sequence[str], None] = '9d2c7fae70a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'league_rosters',
        sa.Column(
            'injury_statuses',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('league_rosters', 'injury_statuses')
