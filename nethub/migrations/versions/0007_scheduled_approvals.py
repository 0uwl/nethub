"""Scheduled approvals: an approval may carry a start time.

Revision ID: 0007_scheduled_approvals
Revises: 0006_canary_activation
Create Date: 2026-09-30

- `upgrade_phase_jobs.not_before`: the earliest the sibling may claim this
  job, in UTC. Null on every existing job, which is what "start now" means,
  so nothing has to be backfilled.

A nullable column with no constraint and no vocabulary change, so SQLite's
ADD COLUMN takes it without rebuilding the table -- which also means the
terminal-status trigger is untouched (see 0002 and 0006, where it was not).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0007_scheduled_approvals'
down_revision = '0006_canary_activation'
branch_labels = None
depends_on = None

#: Frozen copy of the trigger as 0006 left it. The upgrade above does not
#: rebuild the table, but the downgrade does, and a rebuild drops the trigger
#: with the old table.
TERMINAL_TRIGGER = """
CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
BEFORE UPDATE ON upgrade_phase_jobs
FOR EACH ROW WHEN OLD.status IN ('abandoned', 'cancelled', 'expired', 'failed', 'partial', 'succeeded', 'timed_out')
BEGIN
    SELECT RAISE(ABORT,
        'upgrade_phase_jobs row is terminal and must not be updated');
END;
"""


def upgrade():
    op.add_column('upgrade_phase_jobs', sa.Column('not_before', sa.DateTime(),
                                                  nullable=True))


def downgrade():
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_column('not_before')
    op.execute(TERMINAL_TRIGGER)
