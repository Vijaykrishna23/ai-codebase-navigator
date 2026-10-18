"""edges between files and symbols

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "edges",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "src_file_id",
            sa.BigInteger(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "src_symbol_id",
            sa.BigInteger(),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
        ),
        sa.Column("src_name", sa.Text()),
        sa.Column(
            "dst_file_id",
            sa.BigInteger(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "dst_symbol_id",
            sa.BigInteger(),
            sa.ForeignKey("symbols.id", ondelete="CASCADE"),
        ),
        sa.Column("dst_name", sa.Text(), nullable=False),
        sa.Column("line", sa.Integer()),
        sa.Column("confidence", sa.String(10), nullable=False),
    )
    op.create_index("ix_edges_repo_kind", "edges", ["repo_id", "kind"])
    op.create_index("ix_edges_src_symbol", "edges", ["src_symbol_id"])
    op.create_index("ix_edges_dst_symbol", "edges", ["dst_symbol_id"])
    op.create_index("ix_edges_src_file", "edges", ["src_file_id"])
    op.create_index("ix_edges_dst_file", "edges", ["dst_file_id"])


def downgrade() -> None:
    op.drop_table("edges")
