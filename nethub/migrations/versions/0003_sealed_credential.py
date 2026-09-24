"""Add upgrade_phase_jobs.sealed_credential (PLAN.md WS-7).

Revision ID: 0003_sealed_credential
Revises: 0002_internal_failure_stage
Create Date: 2026-09-24

The device credential an approval supplies now travels sealed in the job row
instead of over the credential socket (nethub/sealed_credentials.py). A CHECK
constraint keeps the ciphertext to queued jobs only. SQLite cannot add a
constraint to an existing table, so the table is rebuilt (batch mode), and
the terminal-status trigger, which is dropped with the old table, is
recreated.

Existing rows need nothing: the column is null for every job, which is right
for all of them -- a job queued under the socket design had its credential in
the old web process's memory, which the upgrade's restart discarded. Such a
job fails with failure_stage='credential' when the sibling reaches it, as it
would have after any restart before this change. Upgrade with no phase
queued or running (README, "Upgrading NetHub").
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0003_sealed_credential'
down_revision = '0002_internal_failure_stage'
branch_labels = None
depends_on = None

CHECK_NAME = 'ck_sealed_credential_only_while_queued'
CHECK_SQL = "status = 'queued' OR sealed_credential IS NULL"

#: Frozen copy of the trigger from 0001_baseline.
TERMINAL_TRIGGER = """
CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
BEFORE UPDATE ON upgrade_phase_jobs
FOR EACH ROW WHEN OLD.status IN ('abandoned', 'cancelled', 'expired', 'failed', 'succeeded', 'timed_out')
BEGIN
    SELECT RAISE(ABORT,
        'upgrade_phase_jobs row is terminal and must not be updated');
END;
"""


def upgrade():
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.add_column(sa.Column('sealed_credential', sa.LargeBinary(), nullable=True))
        batch_op.create_check_constraint(CHECK_NAME, CHECK_SQL)
    op.execute(TERMINAL_TRIGGER)


def downgrade():
    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_constraint(CHECK_NAME, type_='check')
        batch_op.drop_column('sealed_credential')
    op.execute(TERMINAL_TRIGGER)
