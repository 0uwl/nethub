"""Add job status `partial` and `upgrade_phase_jobs.is_retry` (PLAN.md WS-8).

Revision ID: 0004_partial_and_retry
Revises: 0003_sealed_credential
Create Date: 2026-09-25

A phase where some hosts fail no longer fails the run: the job ends `partial`,
the run carries on with the hosts that passed, and a retry (a job with
`is_retry` set) runs the phase again on the ones that failed.

`partial` is a new value in the status CHECK constraint and a new terminal
status, so the table is rebuilt (SQLite cannot alter a constraint in place;
see 0002), the old named CHECK is dropped first, and the terminal-status
trigger is recreated with `partial` in its list. The new column has a server
default, so existing rows read as not retries, which they are not.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0004_partial_and_retry'
down_revision = '0003_sealed_credential'
branch_labels = None
depends_on = None

OLD = ('queued', 'running', 'succeeded', 'failed', 'timed_out', 'abandoned',
       'cancelled', 'expired')
NEW = ('queued', 'running', 'succeeded', 'partial', 'failed', 'timed_out', 'abandoned',
       'cancelled', 'expired')

#: Frozen copies of the trigger before and after: the terminal list is every
#: status but `queued` and `running`, sorted.
TRIGGER = """
CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
BEFORE UPDATE ON upgrade_phase_jobs
FOR EACH ROW WHEN OLD.status IN ({terminal})
BEGIN
    SELECT RAISE(ABORT,
        'upgrade_phase_jobs row is terminal and must not be updated');
END;
"""


def _trigger(statuses):
    return TRIGGER.format(terminal=', '.join(f"'{s}'" for s in sorted(statuses[2:])))


def _status_type(values):
    return sa.Enum(*values, name='job_status', native_enum=False, create_constraint=True)


def upgrade():
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_constraint('job_status', type_='check')
        batch_op.alter_column('status', existing_type=_status_type(OLD),
                              type_=_status_type(NEW), existing_nullable=False)
        batch_op.add_column(sa.Column('is_retry', sa.Boolean(), nullable=False,
                                      server_default='0'))
    op.execute(_trigger(NEW))


def downgrade():
    # Only possible while no row is `partial`; the CHECK on the rebuilt table
    # rejects the copy otherwise, and the migration rolls back whole.
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_constraint('job_status', type_='check')
        batch_op.alter_column('status', existing_type=_status_type(NEW),
                              type_=_status_type(OLD), existing_nullable=False)
        batch_op.drop_column('is_retry')
    op.execute(_trigger(OLD))
