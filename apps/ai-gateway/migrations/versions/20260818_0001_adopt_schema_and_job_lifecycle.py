"""Adopt the existing schema and add a reliable job lifecycle.

Revision ID: 20260818_0001
Revises:
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa

revision = "20260818_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # A fresh installation is created from the current metadata. On an
    # existing installation this revision adopts the schema in place and only
    # adds missing columns, so no user data is rewritten or dropped.
    from app.database import Base
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=bind)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("jobs")}

    additions = [
        ("metadata_json", sa.Column("metadata_json", sa.Text(), nullable=True)),
        ("phase", sa.Column("phase", sa.String(length=50), nullable=False, server_default="queued")),
        ("progress", sa.Column("progress", sa.Integer(), nullable=False, server_default="0")),
        ("progress_message", sa.Column("progress_message", sa.Text(), nullable=True)),
        ("attempts", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")),
        ("heartbeat_at", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)),
        ("started_at", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)),
        ("completed_at", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)),
        ("cancel_requested_at", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("jobs", column)


def downgrade() -> None:
    bind = op.get_bind()
    if "jobs" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("jobs")}
    for name in (
        "cancel_requested_at",
        "completed_at",
        "started_at",
        "heartbeat_at",
        "attempts",
        "progress_message",
        "progress",
        "phase",
    ):
        if name in columns:
            op.drop_column("jobs", name)
