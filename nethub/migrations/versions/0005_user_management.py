"""Disable users, revoke sessions, audit user administration (PLAN.md WS-10).

Revision ID: 0005_user_management
Revises: 0004_partial_and_retry
Create Date: 2026-09-26

- `user.is_active`: deactivation rather than deletion (design doc §5). Every
  existing user stays active.
- `user.session_epoch`: part of the id in the session cookie, bumped to
  revoke a user's sessions. Existing cookies carry a bare id, which the new
  user loader refuses, so everyone logs in again once after this upgrade.
- `user_admin_audit`, append-only by trigger as design doc §5 specifies for
  it.

Both columns are added with a server default, which SQLite's ADD COLUMN
accepts without rebuilding the table.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0005_user_management'
down_revision = '0004_partial_and_retry'
branch_labels = None
depends_on = None

ACTIONS = ('created', 'password_changed', 'password_reset', 'disabled', 'enabled',
           'unlocked')

#: Frozen copy of the model's triggers.
TRIGGER = """
CREATE TRIGGER user_admin_audit_no_{name}
BEFORE {event} ON user_admin_audit
BEGIN
    SELECT RAISE(ABORT, 'user_admin_audit is append-only');
END;
"""


def upgrade():
    op.add_column('user', sa.Column('is_active', sa.Boolean(), nullable=False,
                                    server_default='1'))
    op.add_column('user', sa.Column('session_epoch', sa.Integer(), nullable=False,
                                    server_default='0'))
    op.create_table(
        'user_admin_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('target_user_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.Enum(*ACTIONS, name='user_audit_action', native_enum=False,
                                    create_constraint=True), nullable=False),
        sa.Column('detail', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['actor_user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['target_user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    for event in ('UPDATE', 'DELETE'):
        op.execute(TRIGGER.format(name=event.lower(), event=event))


def downgrade():
    op.drop_table('user_admin_audit')
    with op.batch_alter_table('user', recreate='always') as batch_op:
        batch_op.drop_column('session_epoch')
        batch_op.drop_column('is_active')
