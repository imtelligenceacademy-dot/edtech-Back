"""chat threads per class

A teacher who takes the same grade more than once teaches the same lesson to
each of their classes, and asks the assistant different things in each room.
Chat was keyed by (teacher, lesson), so opening a lesson for 6B brought back
the conversation from 6A — and clearing the thread in one room wiped it for
all of them.

`chat_messages.section` says which class a turn belongs to, and the thread
index leads with it so a class's conversation is still one lookup. Existing
messages take the empty-string class, which is the single unnamed class every
teacher who takes a grade once has, so nothing already said moves or is lost.

Revision ID: c47f0a6e21b8
Revises: 9b3d71c8a4e5
Create Date: 2026-09-04 18:41:09.552310

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c47f0a6e21b8'
down_revision: Union[str, None] = '9b3d71c8a4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'section',
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch_op.drop_index('ix_chat_thread')
        batch_op.create_index(
            'ix_chat_thread',
            ['teacher_id', 'lesson_id', 'section', 'created_at'],
            unique=False,
        )


def downgrade() -> None:
    # Every class's messages survive; they simply merge back into one thread per
    # lesson, which is what the old schema could express.
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_index('ix_chat_thread')
        batch_op.create_index(
            'ix_chat_thread',
            ['teacher_id', 'lesson_id', 'created_at'],
            unique=False,
        )
        batch_op.drop_column('section')
