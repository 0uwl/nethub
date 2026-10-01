"""Roles, settings and the two-person rules (PLAN.md WS-16).

Revision ID: 0008_roles_and_settings
Revises: 0007_scheduled_approvals
Create Date: 2026-10-01

- `user.role`, `admin` or `operator`. Every user that exists now becomes an
  admin, which is what everyone was before roles, so nobody loses access.
- `role_changed` joins `user_admin_audit.action`.
- `settings` and the append-only `settings_audit` (design doc §5), holding the
  three two-person rules for now.
- `artifacts.published_by`/`published_at` and `delete_requested_by`/`_at`, and
  an append-only `artifact_audit` that outlives the row a delete removes.
- `device_host_keys.delete_requested_by`/`_at`; `device_host_key_audit` gains
  `requested_by`, `actor_role` and the `delete_requested` action.
- `upgrade_phase_jobs.approved_by_role` and `device_username_used`, the
  supplier's device username for that phase.

The new columns carry foreign keys or CHECK constraints, and the changed
vocabularies are CHECK constraints, so the tables are rebuilt rather than
altered (see 0002). A rebuild drops a table's triggers with the old table:
`user_admin_audit`'s append-only pair and `upgrade_phase_jobs`' terminal
trigger are recreated here.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0008_roles_and_settings'
down_revision = '0007_scheduled_approvals'
branch_labels = None
depends_on = None

ROLES = ('admin', 'operator')
OLD_USER_ACTIONS = ('created', 'password_changed', 'password_reset', 'disabled', 'enabled',
                    'unlocked')
NEW_USER_ACTIONS = OLD_USER_ACTIONS + ('role_changed',)
OLD_HOSTKEY_ACTIONS = ('confirmed', 'deleted')
NEW_HOSTKEY_ACTIONS = ('confirmed', 'delete_requested', 'deleted')
ARTIFACT_ACTIONS = ('uploaded', 'published', 'withdrawn', 'delete_requested', 'deleted')

#: Frozen copies of the models' triggers.
APPEND_ONLY_TRIGGER = """
CREATE TRIGGER {table}_no_{name}
BEFORE {event} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only');
END;
"""

TERMINAL_TRIGGER = """
CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
BEFORE UPDATE ON upgrade_phase_jobs
FOR EACH ROW WHEN OLD.status IN ('abandoned', 'cancelled', 'expired', 'failed', 'partial', 'succeeded', 'timed_out')
BEGIN
    SELECT RAISE(ABORT,
        'upgrade_phase_jobs row is terminal and must not be updated');
END;
"""


def _enum(values, name):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _append_only(table):
    for event in ('UPDATE', 'DELETE'):
        op.execute(APPEND_ONLY_TRIGGER.format(table=table, name=event.lower(), event=event))


def _retype(batch_op, column, constraint, values_from, values_to, nullable):
    batch_op.drop_constraint(constraint, type_='check')
    batch_op.alter_column(column, existing_type=_enum(values_from, constraint),
                          type_=_enum(values_to, constraint), existing_nullable=nullable)


def upgrade():
    with op.batch_alter_table('user', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('role', _enum(ROLES, 'user_role'), nullable=False,
                                      server_default='operator'))
    # Everyone was an admin before roles existed.
    op.execute("UPDATE user SET role = 'admin'")

    with op.batch_alter_table('user_admin_audit', recreate='always') as batch_op:
        _retype(batch_op, 'action', 'user_audit_action', OLD_USER_ACTIONS,
                NEW_USER_ACTIONS, nullable=False)
    _append_only('user_admin_audit')

    op.create_table(
        'settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=255), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['user.id']),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table(
        'settings_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('old_value', sa.String(length=255), nullable=True),
        sa.Column('new_value', sa.String(length=255), nullable=False),
        sa.Column('changed_by', sa.Integer(), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['changed_by'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    _append_only('settings_audit')

    with op.batch_alter_table('artifacts', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('published_by', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_artifacts_published_by', 'user',
                                    ['published_by'], ['id'])
        batch_op.add_column(sa.Column('published_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('delete_requested_by', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_artifacts_delete_requested_by', 'user',
                                    ['delete_requested_by'], ['id'])
        batch_op.add_column(sa.Column('delete_requested_at', sa.DateTime(), nullable=True))
    # Every existing artifact is published, by whoever uploaded it.
    op.execute("UPDATE artifacts SET published_by = uploaded_by, published_at = uploaded_at "
               "WHERE state = 'published'")

    op.create_table(
        'artifact_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('artifact_id', sa.Integer(), nullable=False),
        sa.Column('bundle_key', sa.String(length=80), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('sha512', sa.String(length=128), nullable=False),
        sa.Column('action', _enum(ARTIFACT_ACTIONS, 'artifact_audit_action'), nullable=False),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('actor_role', _enum(ROLES, 'artifact_audit_actor_role'), nullable=True),
        sa.ForeignKeyConstraint(['actor_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    _append_only('artifact_audit')

    with op.batch_alter_table('device_host_keys', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('delete_requested_by', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_device_host_keys_delete_requested_by', 'user',
                                    ['delete_requested_by'], ['id'])
        batch_op.add_column(sa.Column('delete_requested_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('device_host_key_audit', recreate='always') as batch_op:
        _retype(batch_op, 'action', 'hostkey_audit_action', OLD_HOSTKEY_ACTIONS,
                NEW_HOSTKEY_ACTIONS, nullable=False)
        batch_op.add_column(sa.Column('requested_by', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_device_host_key_audit_requested_by', 'user',
                                    ['requested_by'], ['id'])
        batch_op.add_column(sa.Column('actor_role', _enum(ROLES, 'hostkey_audit_actor_role'),
                                      nullable=True))

    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('approved_by_role', _enum(ROLES, 'job_approved_by_role'),
                                      nullable=True))
        batch_op.add_column(sa.Column('device_username_used', sa.String(length=80),
                                      nullable=True))
    op.execute(TERMINAL_TRIGGER)


def downgrade():
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_column('device_username_used')
        batch_op.drop_constraint('job_approved_by_role', type_='check')
        batch_op.drop_column('approved_by_role')
    op.execute(TERMINAL_TRIGGER)

    # Only possible while no row says `delete_requested`; the CHECK on the
    # rebuilt table rejects the copy otherwise, and the migration rolls back.
    with op.batch_alter_table('device_host_key_audit', recreate='always') as batch_op:
        batch_op.drop_constraint('hostkey_audit_actor_role', type_='check')
        batch_op.drop_column('actor_role')
        batch_op.drop_column('requested_by')
        _retype(batch_op, 'action', 'hostkey_audit_action', NEW_HOSTKEY_ACTIONS,
                OLD_HOSTKEY_ACTIONS, nullable=False)

    with op.batch_alter_table('device_host_keys', recreate='always') as batch_op:
        batch_op.drop_column('delete_requested_at')
        batch_op.drop_column('delete_requested_by')

    op.drop_table('artifact_audit')
    with op.batch_alter_table('artifacts', recreate='always') as batch_op:
        batch_op.drop_column('delete_requested_at')
        batch_op.drop_column('delete_requested_by')
        batch_op.drop_column('published_at')
        batch_op.drop_column('published_by')

    op.drop_table('settings_audit')
    op.drop_table('settings')

    with op.batch_alter_table('user_admin_audit', recreate='always') as batch_op:
        _retype(batch_op, 'action', 'user_audit_action', NEW_USER_ACTIONS,
                OLD_USER_ACTIONS, nullable=False)
    _append_only('user_admin_audit')

    with op.batch_alter_table('user', recreate='always') as batch_op:
        batch_op.drop_constraint('user_role', type_='check')
        batch_op.drop_column('role')
