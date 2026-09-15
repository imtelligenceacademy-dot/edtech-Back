"""record when a run of failed logins began

`users.failed_login_count` had no companion timestamp, so there was no way to
tell five wrong passwords in a minute from five spread across a term. The count
only ever climbed — nothing but a successful sign-in or an admin reset brought
it down — and the lockout it feeds grows with it, so a slow trickle of attempts
against a known email reached the 24-hour cap and stayed there.

This column anchors the run. A failure that arrives after the window has passed
starts a new one from zero, which is what makes the escalation describe a burst
rather than an account's whole history.

Nullable, and null on every existing row: no run is in progress at the moment
this is added, and the first failure after it will start one.

Revision ID: f3a91c47b2e5
Revises: b7e4c2915d30
Create Date: 2026-09-15 16:41:08.552317

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a91c47b2e5'
down_revision: Union[str, None] = 'b7e4c2915d30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('failed_login_window_started_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('failed_login_window_started_at')
