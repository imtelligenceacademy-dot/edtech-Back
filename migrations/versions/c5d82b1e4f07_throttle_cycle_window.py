"""let a network's failed-login cycles age, and remember its successes

`login_throttles.cycle_count` is the only route to the 24-hour network ban, and
nothing but a successful sign-in ever brought it down. That is wrong in both
directions at once: on a shared address — a school where every teacher signs in
through one NAT — somebody succeeds every few minutes, so the count never
reached two and the ban was unreachable on exactly the deployments it was
written for; while on an address nobody ever signs in from, the count was kept
for good.

`cycle_started_at` anchors a run of cycles so it can age out on its own.
`last_success_at` records that the address does sign people in, which is read
when a ban is being decided rather than used to erase what happened.

Both nullable and null on every existing row: no run is in progress at the
moment this is added, and an address with no recorded success simply has not
had one seen yet — which, for the ban decision, is the same answer it would
have given before.

Revision ID: c5d82b1e4f07
Revises: f3a91c47b2e5
Create Date: 2026-09-15 16:52:19.447031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d82b1e4f07'
down_revision: Union[str, None] = 'f3a91c47b2e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('login_throttles', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cycle_started_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('login_throttles', schema=None) as batch_op:
        batch_op.drop_column('last_success_at')
        batch_op.drop_column('cycle_started_at')
