"""retire "late"

Nothing in the curriculum is late. A teacher works through it at the pace their
classes allow, and a lesson they have not reached yet is where they are, not a
failing — but the watchdog compared the lesson's due date against today and put
"Late" against them for it, in the school report and on the progress screens.

The code no longer produces the value. This clears the rows that already hold
it, so the word is gone from the product rather than merely from new writes:

- `progress.watchdog` 'late' becomes 'on-track'
- `progress.status` 'late' becomes 'in-progress'

Neither touches `percent_complete`, so what a teacher actually did is unchanged
— only the verdict attached to it.

Revision ID: d8b21c60fa73
Revises: c47f0a6e21b8
Create Date: 2026-09-10 21:18:02.114509

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd8b21c60fa73'
down_revision: Union[str, None] = 'c47f0a6e21b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE progress SET watchdog = 'on-track' WHERE watchdog = 'late'")
    op.execute("UPDATE progress SET status = 'in-progress' WHERE status = 'late'")


def downgrade() -> None:
    # Deliberately empty. Which rows were called late was a judgement the app
    # made from a due date, not a fact anybody recorded, so it cannot be
    # reconstructed — and re-deriving it would put the word back in front of
    # teachers, which is the thing this removed.
    pass
