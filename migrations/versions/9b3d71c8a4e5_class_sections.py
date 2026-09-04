"""class sections

One teacher often takes the same grade more than once — 6A, 6B, 6C — and each of
those classes moves through the curriculum at its own pace. Progress was unique
on (teacher, lesson), so those classes shared one row: marking a lesson complete
after teaching 6A locked it, leaving the teacher unable to reopen the material
she still had to teach three more times, and started the next lesson's unlock
countdown for classes that had not had this one.

This adds the missing dimension:

- `users.sections` holds the classes a super-admin has named per grade, e.g.
  {"G6": ["A", "B", "C", "D"]}. Absent or empty means one unnamed class.
- `progress.section` and `access_requests.section` say which class a row is for.
- Progress becomes unique on (teacher, lesson, section).

Both new columns default to the empty string, not NULL. SQLite and Postgres each
treat NULLs as distinct inside a UNIQUE constraint, so a nullable column would
quietly allow duplicate progress rows for the same teacher and lesson — exactly
the thing the constraint exists to prevent. Every existing row takes that empty
default, which is the unnamed single class, so teachers who take one class per
grade are unaffected by this migration and see no change.

Revision ID: 9b3d71c8a4e5
Revises: 42958f0e7fe1
Create Date: 2026-09-04 10:12:44.301887

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b3d71c8a4e5'
down_revision: Union[str, None] = '42958f0e7fe1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'sections',
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    with op.batch_alter_table('access_requests', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'section',
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("''"),
            )
        )

    # The column has to exist before it can join the unique constraint, and the
    # old constraint has to go before the new one lands — on SQLite batch mode
    # rebuilds the table once for the whole block.
    with op.batch_alter_table('progress', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'section',
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch_op.drop_constraint('uq_teacher_lesson', type_='unique')
        batch_op.create_unique_constraint(
            'uq_teacher_lesson_section', ['teacher_id', 'lesson_id', 'section']
        )


def downgrade() -> None:
    # Going back collapses each teacher's classes into one row per lesson. Only
    # the first class of each survives; the rest are dropped, because the old
    # constraint has no room for them. That loses real teaching history, so this
    # is a rollback of last resort rather than a routine reversal.
    op.execute(
        """
        DELETE FROM progress
        WHERE id NOT IN (
            SELECT MIN(id) FROM progress GROUP BY teacher_id, lesson_id
        )
        """
    )

    with op.batch_alter_table('progress', schema=None) as batch_op:
        batch_op.drop_constraint('uq_teacher_lesson_section', type_='unique')
        batch_op.create_unique_constraint(
            'uq_teacher_lesson', ['teacher_id', 'lesson_id']
        )
        batch_op.drop_column('section')

    with op.batch_alter_table('access_requests', schema=None) as batch_op:
        batch_op.drop_column('section')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('sections')
