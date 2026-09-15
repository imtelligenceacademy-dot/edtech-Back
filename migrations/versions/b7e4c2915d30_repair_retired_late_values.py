"""repair the rows "retire late" wrote as enum values

`d8b21c60fa73` set `progress.watchdog` to 'on-track' and `progress.status` to
'in-progress'. Those are the enum members' *values*; `Enum(..., native_enum=
False)` persists their *names*, so the rows it meant to fix became rows the ORM
cannot read at all — loading one raises LookupError, and every screen built on
`Progress` (teacher progress, the dashboard, the watchdog, both report
builders) fails with it.

That migration now writes names. This repairs the databases where the earlier
version already ran. A hyphenated string in either column can only have come
from it: every write the application makes goes through the ORM, which stores
`on_track` / `in_progress`. Any surviving 'late' is swept in the same pass, so
the invariant holds whichever version of `d8b21c60fa73` a given database saw,
including one that was interrupted partway.

Revision ID: b7e4c2915d30
Revises: d8b21c60fa73
Create Date: 2026-09-15 10:24:41.882106

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e4c2915d30'
down_revision: Union[str, None] = 'd8b21c60fa73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE progress SET watchdog = 'on_track'"
        " WHERE watchdog IN ('on-track', 'late')"
    )
    op.execute(
        "UPDATE progress SET status = 'in_progress'"
        " WHERE status IN ('in-progress', 'late')"
    )


def downgrade() -> None:
    # Deliberately empty. The only thing to undo here is the corruption, and
    # putting it back would leave the database unreadable by the code that
    # ships alongside this revision.
    pass
