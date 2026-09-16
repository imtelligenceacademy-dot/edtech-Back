"""end the sessions a password reset replaces

Resetting a password revoked the account's refresh tokens, and its docstring
promised that existing sessions must re-authenticate. Only half of that was
true. The access token is a signed JWT held in a cookie: nothing on the server
can revoke it, so whoever held one kept every non-auth endpoint for the rest of
its fifteen minutes. An admin resetting a password because a session was stolen
was closing the door for a quarter of an hour's time.

`sessions_valid_from` is the moment before which this account's access tokens
stop being accepted. The token already carries `iat`, so the check is a
comparison and costs nothing.

Null on every existing row, and null means "nothing has been invalidated" — the
same answer the check would have given before the column existed. No session is
ended by this migration running.

Revision ID: d3940bee30a4
Revises: c5d82b1e4f07
Create Date: 2026-09-16 12:58:04.118392

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3940bee30a4'
down_revision: Union[str, None] = 'c5d82b1e4f07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('sessions_valid_from', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('sessions_valid_from')
