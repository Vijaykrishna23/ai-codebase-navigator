"""repos, files, symbols

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(255)),
        sa.Column("sha", sa.String(40)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("stats", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner", "name"),
    )
    op.create_table(
        "files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(20)),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("loc", sa.Integer(), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("parse_error", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.UniqueConstraint("repo_id", "path"),
    )
    op.create_table(
        "symbols",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.BigInteger(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            sa.BigInteger(),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text()),
        sa.Column("docstring", sa.Text()),
    )
    op.create_index("ix_symbols_repo_name", "symbols", ["repo_id", "name"])
    op.create_index(
        "ix_symbols_repo_qualified_name", "symbols", ["repo_id", "qualified_name"]
    )
    op.create_index("ix_symbols_file", "symbols", ["file_id"])


def downgrade() -> None:
    op.drop_table("symbols")
    op.drop_table("files")
    op.drop_table("repos")
