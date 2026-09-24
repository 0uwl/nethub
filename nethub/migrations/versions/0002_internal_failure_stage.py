"""Add `internal` to the phase failure_stage vocabulary.

Revision ID: 0002_internal_failure_stage
Revises: 0001_baseline
Create Date: 2026-09-24

`Sibling.recover_own()` records an error in NetHub's own code. It used
`connect`, the closest word available, which read as a device connection
problem (PLAN.md "Found while working", from WS-1).

The vocabulary is a CHECK constraint, and SQLite cannot alter a constraint in
place, so both tables that carry it are rebuilt (batch mode: new table, copy,
drop, rename). Written by hand: autogenerate does not see a change to an
allowed-value list. Two things the rebuild would otherwise lose, handled here:

- the old named CHECK constraint is reflected into the new table, so it is
  dropped explicitly before the column gets its new type;
- the terminal-status trigger belongs to the old `upgrade_phase_jobs` table
  and is dropped with it, so it is recreated.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '0002_internal_failure_stage'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None

OLD = ('credential', 'connect', 'hostkey', 'privilege', 'precheck',
       'transfer', 'checksum', 'install', 'reload', 'postcheck')
NEW = OLD + ('internal',)

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

TABLES = (
    ('upgrade_phase_jobs', 'phase_failure_stage'),
    ('upgrade_host_phase_results', 'result_failure_stage'),
)


def _stage_type(values, name):
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _retype(values_from, values_to):
    for table, constraint in TABLES:
        with op.batch_alter_table(table, recreate='always') as batch_op:
            batch_op.drop_constraint(constraint, type_='check')
            batch_op.alter_column(
                'failure_stage',
                existing_type=_stage_type(values_from, constraint),
                type_=_stage_type(values_to, constraint),
                existing_nullable=True,
            )
    op.execute(TERMINAL_TRIGGER)


def upgrade():
    _retype(OLD, NEW)


def downgrade():
    # Only possible while no row uses the new value; the CHECK constraint on
    # the rebuilt table rejects the copy otherwise, and the migration rolls
    # back whole.
    _retype(NEW, OLD)
