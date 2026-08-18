"""Backfill lifecycle data for jobs created before lifecycle tracking.

Revision ID: 20260818_0002
Revises: 20260818_0001
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_0002"
down_revision = "20260818_0001"
branch_labels = None
depends_on = None


jobs = sa.table(
    "jobs",
    sa.column("status", sa.String()),
    sa.column("phase", sa.String()),
    sa.column("progress", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
    sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("completed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    terminal_statuses = ("completed", "failed", "cancelled")

    # Revision 0001 deliberately used harmless defaults while adopting the
    # pre-Alembic database. Reconstruct the display fields for those legacy
    # rows so the new Obsidian job centre does not show completed work at 0%.
    op.execute(
        jobs.update()
        .where(jobs.c.phase == "queued")
        .where(jobs.c.status != "queued")
        .values(phase=jobs.c.status)
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status == "completed")
        .where(jobs.c.progress == 0)
        .values(progress=100)
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status.in_(("processing", *terminal_statuses)))
        .where(jobs.c.started_at.is_(None))
        .values(started_at=jobs.c.created_at)
    )
    op.execute(
        jobs.update()
        .where(jobs.c.status.in_(terminal_statuses))
        .where(jobs.c.completed_at.is_(None))
        .values(completed_at=jobs.c.updated_at)
    )


def downgrade() -> None:
    # This migration only reconstructs metadata from existing timestamps and
    # statuses. Removing that information on downgrade would be destructive.
    pass
