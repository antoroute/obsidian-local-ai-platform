"""Add durable enterprise generation and desktop relay state.

Revision ID: 20261010_0003
Revises: 20260818_0002
"""
from alembic import op

from app.models import GenerationRun, GenerationStep, GenerationUserState, RelaySession

revision = "20261010_0003"
down_revision = "20260818_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for model in (GenerationUserState, RelaySession, GenerationRun, GenerationStep):
        model.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Deployments roll back application images, retaining durable user results.
    raise RuntimeError("Retain enterprise generation tables when rolling back application images.")
