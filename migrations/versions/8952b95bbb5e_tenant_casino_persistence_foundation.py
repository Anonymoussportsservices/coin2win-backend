"""tenant casino persistence foundation

Revision ID: 8952b95bbb5e
Revises: c509118bcb5e
Create Date: 2026-10-04 19:48:44.628371

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '8952b95bbb5e'
down_revision: Union[str, Sequence[str], None] = 'c509118bcb5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenant-aware casino configuration persistence."""

    op.create_table(
        "brand_casino_configurations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'LEGACY'"),
        ),
        sa.Column(
            "provider_config",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "mode IN ('LEGACY', 'TENANT')",
            name="ck_brand_casino_configurations_mode",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brand_domains.id"],
            name="fk_brand_casino_configurations_brand_id",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_brand_casino_configurations",
        ),
        sa.UniqueConstraint(
            "brand_id",
            name="uq_brand_casino_configurations_brand_id",
        ),
    )

    op.create_table(
        "brand_game_configurations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "featured",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("display_title_override", sa.String(length=255), nullable=True),
        sa.Column("category_override", sa.String(length=100), nullable=True),
        sa.Column("image_url_override", sa.Text(), nullable=True),
        sa.Column("presentation_override", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brand_domains.id"],
            name="fk_brand_game_configurations_brand_id",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["softswiss_games.id"],
            name="fk_brand_game_configurations_game_id",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_brand_game_configurations",
        ),
        sa.UniqueConstraint(
            "brand_id",
            "game_id",
            name="uq_brand_game_configurations_brand_game",
        ),
    )


def downgrade() -> None:
    """Remove tenant-aware casino configuration persistence."""

    op.drop_table("brand_game_configurations")
    op.drop_table("brand_casino_configurations")
