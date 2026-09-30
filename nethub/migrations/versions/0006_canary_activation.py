"""Canary activation: host order, a chosen reload count, and `store` (PLAN.md WS-15).

Revision ID: 0006_canary_activation
Revises: 0005_user_management
Create Date: 2026-09-30

- `upgrade_run_hosts.position`: the host's line in the request. Activate
  upgrades the first eligible host alone as a canary, so request order has to
  be in the rows. Existing rows are numbered in insertion order (`rowid`),
  which is the order `submit` inserted them, i.e. request order. Then NOT NULL
  and `UNIQUE(run_id, position)`, which needs a table rebuild.
- `upgrade_phase_jobs.concurrency`: how many devices an activate reloads at
  once after the canary, as approved. Null on every existing job, which is
  what it means for every phase but activate.
- `store` joins the failure_stage vocabulary on both tables that carry it: the
  image in NetHub's own store missing or altered, found before stage touches a
  device.

The vocabulary is a CHECK constraint and SQLite cannot alter one in place, so
both tables are rebuilt with the old named CHECK dropped first, and the
terminal-status trigger, which goes with the old `upgrade_phase_jobs`, is
recreated (see 0002).
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0006_canary_activation'
down_revision = '0005_user_management'
branch_labels = None
depends_on = None

OLD_STAGES = ('credential', 'connect', 'hostkey', 'privilege', 'precheck',
              'transfer', 'checksum', 'install', 'reload', 'postcheck', 'internal')
NEW_STAGES = OLD_STAGES + ('store',)

CONCURRENCY_CHECK = 'ck_phase_job_concurrency_positive'
POSITION_UNIQUE = 'uq_run_host_position'

#: Frozen copy of the trigger as 0004 left it: every status but `queued` and
#: `running` is terminal.
TERMINAL_TRIGGER = """
CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
BEFORE UPDATE ON upgrade_phase_jobs
FOR EACH ROW WHEN OLD.status IN ('abandoned', 'cancelled', 'expired', 'failed', 'partial', 'succeeded', 'timed_out')
BEGIN
    SELECT RAISE(ABORT,
        'upgrade_phase_jobs row is terminal and must not be updated');
END;
"""


def _stage_type(values, name):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _retype_stage(batch_op, constraint, values_from, values_to):
    batch_op.drop_constraint(constraint, type_='check')
    batch_op.alter_column(
        'failure_stage',
        existing_type=_stage_type(values_from, constraint),
        type_=_stage_type(values_to, constraint),
        existing_nullable=True,
    )


def upgrade():
    op.add_column('upgrade_run_hosts', sa.Column('position', sa.Integer(), nullable=True))
    op.execute("""
        UPDATE upgrade_run_hosts
        SET position = (
            SELECT count(*) FROM upgrade_run_hosts AS earlier
            WHERE earlier.run_id = upgrade_run_hosts.run_id
              AND earlier.rowid < upgrade_run_hosts.rowid
        )
    """)
    with op.batch_alter_table('upgrade_run_hosts', recreate='always') as batch_op:
        batch_op.alter_column('position', existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(POSITION_UNIQUE, ['run_id', 'position'])

    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        _retype_stage(batch_op, 'phase_failure_stage', OLD_STAGES, NEW_STAGES)
        batch_op.add_column(sa.Column('concurrency', sa.Integer(), nullable=True))
        batch_op.create_check_constraint(CONCURRENCY_CHECK,
                                         'concurrency IS NULL OR concurrency >= 1')
    op.execute(TERMINAL_TRIGGER)

    with op.batch_alter_table('upgrade_host_phase_results', recreate='always') as batch_op:
        _retype_stage(batch_op, 'result_failure_stage', OLD_STAGES, NEW_STAGES)


def downgrade():
    # Only possible while no row says `store`; the CHECK on the rebuilt tables
    # rejects the copy otherwise, and the migration rolls back whole.
    with op.batch_alter_table('upgrade_host_phase_results', recreate='always') as batch_op:
        _retype_stage(batch_op, 'result_failure_stage', NEW_STAGES, OLD_STAGES)

    with op.batch_alter_table('upgrade_phase_jobs', recreate='always') as batch_op:
        batch_op.drop_constraint(CONCURRENCY_CHECK, type_='check')
        batch_op.drop_column('concurrency')
        _retype_stage(batch_op, 'phase_failure_stage', NEW_STAGES, OLD_STAGES)
    op.execute(TERMINAL_TRIGGER)

    with op.batch_alter_table('upgrade_run_hosts', recreate='always') as batch_op:
        batch_op.drop_constraint(POSITION_UNIQUE, type_='unique')
        batch_op.drop_column('position')
