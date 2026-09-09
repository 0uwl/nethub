from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # The name this person logs into *devices* with -- separate from their
    # NetHub login, and mapped server-side so a submitted request can never
    # assert it (design doc §4.3). Null until set: an upgrade cannot be
    # submitted without it, because the two-sided attribution property is
    # what the whole credential path is built for.
    device_username = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Registry(db.Model):
    """A tracked `software_registry` block inside some file under
    REGISTRIES_ROOT. NetHub owns this row, not the file -- deleting a
    Registry only forgets the pointer (see nethub/registry.py).
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    # Relative to REGISTRIES_ROOT -- never store an absolute path here, it's
    # re-joined against the (possibly redeployed) root at every use.
    file_path = db.Column(db.String(255), unique=True, nullable=False)
    # Absolute path where this registry's images live -- may be outside
    # REGISTRIES_ROOT entirely (see nethub/registry.py's path-safety note).
    search_dir = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Software Lifecycle: upgrade runs, their phases, and the host keys a run may
# target. Design doc §5 for the columns, §7.3 for the three state machines and
# who writes each edge, §8.1 for the phase model itself.
#
# Two deviations from §5's column lists, both because the EE is gone
# (netmiko.md): there is no `private_data_dir` -- nothing renders a directory
# for an execution to read -- and `playbook_log_path` is `log_path`, since
# what it points at is our own transcript rather than a playbook's.
# ---------------------------------------------------------------------------

#: §8.1's phases, in order. `cleanup` is optional; declining it closes the run.
PHASES = ('precheck', 'stage', 'activate', 'verify', 'cleanup')

#: §7.3 job status, shared by every job kind so one sweep serves them all.
JOB_STATUSES = (
    'queued', 'running',
    'succeeded', 'failed', 'timed_out', 'abandoned', 'cancelled', 'expired',
)
TERMINAL_JOB_STATUSES = frozenset(JOB_STATUSES[2:])

#: §7.3 run state. `pre_checking` is distinct from `running` because pre-check
#: needs no gate, and it has its own failure/cancel exits for the same reason.
RUN_STATES = (
    'pre_checking', 'awaiting_approval', 'running',
    'completed', 'failed', 'cancelled', 'expired',
)

#: §7.3 per-host cursor. The per-phase detail is UpgradeHostPhaseResult; this
#: column is a cursor, not the record of what happened.
HOST_STATES = (
    'pending', 'precheck_ok', 'staged', 'activated', 'verified',
    'failed', 'skipped',
)

#: §7.3 failure_stage, phase-job vocabulary. Deliberately *not* shared with the
#: publish side: a publish stops at promote/render/commit, and §8.1's phases are
#: themselves named `stage` and `verify`, so one vocabulary would produce a row
#: reading phase='activate', failure_stage='stage' that is ambiguous on its face.
PHASE_FAILURE_STAGES = (
    'credential', 'connect', 'hostkey', 'privilege', 'precheck',
    'transfer', 'checksum', 'install', 'reload', 'postcheck',
)

TRANSPORTS = ('push_scp', 'pull_sftp')


def _enum(values, name):
    # native_enum=False keeps this a VARCHAR plus a CHECK constraint, which is
    # what SQLite can actually enforce -- and `create_constraint=True` is not
    # optional decoration: SQLAlchemy has defaulted it to False since 1.4, so
    # without it these columns are plain strings and every vocabulary below is
    # documentation rather than a constraint. Verified by reading the emitted
    # DDL, not by assuming.
    return db.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _utcnow():
    return datetime.now(timezone.utc)


def is_terminal(status):
    return status in TERMINAL_JOB_STATUSES


class DeviceHostKey(db.Model):
    """What answered at an address, so a change in the answer is visible.

    Keyed on the address rather than on a device identity on purpose: NetHub is
    not an inventory (design doc §2). Uniqueness is on `ansible_host` alone with
    one pinned `key_type`, not on the pair -- keyed on the pair, an attacker who
    wants a fresh first-contact prompt gets one just by offering an unpinned
    algorithm (§5).

    `first_seen_at` records the raw first contact; `confirmed_by`/`confirmed_at`
    record a human accepting it. They are separate because a run may only name
    an address that a person has already confirmed (§4.3) -- an unconfirmed row
    is not a usable pin. These rows are exempt from §7.4's retention purge:
    expiring one silently downgrades a fail-closed mismatch back to a
    first-contact prompt.
    """

    __tablename__ = 'device_host_keys'

    id = db.Column(db.Integer, primary_key=True)
    ansible_host = db.Column(db.String(64), unique=True, nullable=False)
    key_type = db.Column(db.String(32), nullable=False)
    fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    confirmed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    confirmed_at = db.Column(db.DateTime)

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None


class UpgradeRun(db.Model):
    """One upgrade dispatch: a bundle across a set of devices (§8.1)."""

    __tablename__ = 'upgrade_runs'

    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(32), nullable=False, default='iosxe')
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Snapshots, not lookups. An audit row that re-reads its own answer from a
    # mutable table stops being an audit row the first time somebody's mapping
    # is corrected (§5).
    device_username_used = db.Column(db.String(80), nullable=False)
    #: Whether that name came from the submitter or from shared account mode.
    #: Without it an auditor cannot tell "jsmith ran this" from "everyone runs
    #: as jsmith", which erases the property §4.3 exists to build.
    shared_account_mode = db.Column(db.Boolean, nullable=False, default=False)
    #: A run parks at gates for days, so a settings change mid-run is ordinary.
    #: Snapshotting the transport is what keeps scp_restore_confirmed readable.
    image_transport_used = db.Column(_enum(TRANSPORTS, 'transport'), nullable=False)
    distribution_host_used = db.Column(db.String(255))

    request_document = db.Column(db.Text, nullable=False)
    request_sha512 = db.Column(db.String(128), nullable=False)

    state = db.Column(_enum(RUN_STATES, 'run_state'), nullable=False, default='pre_checking')
    #: Which gate a run at `awaiting_approval` is sitting at. Inferring it from
    #: the highest phase row present re-encodes §8.1's order in application
    #: code, and cannot distinguish "awaiting cleanup" from "cleanup declined".
    awaiting_phase = db.Column(_enum(PHASES, 'awaiting_phase'))
    gate_expires_at = db.Column(db.DateTime)

    cancel_requested_at = db.Column(db.DateTime)
    cancel_requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    finished_at = db.Column(db.DateTime)

    hosts = db.relationship('UpgradeRunHost', backref='run', cascade='all, delete-orphan')
    phase_jobs = db.relationship('UpgradePhaseJob', backref='run', cascade='all, delete-orphan')


class UpgradeRunHost(db.Model):
    """One targeted device, with the bundle it was targeted at snapshotted.

    `file_size` is here so a phase can render what it needs from the run's own
    rows: re-reading it from the artifact would let a mid-run supersede
    silently re-target the run (§5).
    """

    __tablename__ = 'upgrade_run_hosts'

    run_id = db.Column(db.Integer, db.ForeignKey('upgrade_runs.id'), primary_key=True)
    hostname = db.Column(db.String(255), primary_key=True)
    ansible_host = db.Column(db.String(64), nullable=False)

    #: No artifacts table until build step 7, so this is deliberately a bare
    #: integer rather than a foreign key. Don't read it as one.
    artifact_id = db.Column(db.Integer)
    bundle_key = db.Column(db.String(80))
    filename = db.Column(db.String(255), nullable=False)
    sha512 = db.Column(db.String(128), nullable=False)
    version = db.Column(db.String(32), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False)
    flash_dir = db.Column(db.String(64), nullable=False, default='flash:')

    config_backup_path = db.Column(db.String(255))
    reported_version_pre = db.Column(db.String(32))
    reported_version_post = db.Column(db.String(32))

    state = db.Column(_enum(HOST_STATES, 'host_state'), nullable=False, default='pending')
    last_phase = db.Column(_enum(PHASES, 'host_last_phase'))
    error_summary = db.Column(db.String(500))


class UpgradePhaseJob(db.Model):
    """One phase execution. Flask writes exactly one edge here -- the row's
    creation at `queued`; everything after dispatch belongs to the sibling
    (§7.3, §9).
    """

    __tablename__ = 'upgrade_phase_jobs'
    __table_args__ = (
        # The mutex the gate actually needs. §8.1's serialization is scoped to
        # *execution*, so it stops two processes overlapping, not two rows
        # being created -- two admins both clicking "approve: reload" would
        # otherwise queue two reloads. `attempt` is what keeps that constraint
        # from also forbidding the fresh retry §7.3 grants an abandoned phase.
        db.UniqueConstraint('run_id', 'phase', 'attempt', name='uq_phase_job_attempt'),
    )

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('upgrade_runs.id'), nullable=False)
    phase = db.Column(_enum(PHASES, 'job_phase'), nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1)

    #: Null for pre-check alone, which runs on submit with no gate (§8.1).
    #: For every other phase these are what makes an approval a row rather
    #: than a keystroke.
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    approved_at = db.Column(db.DateTime)

    status = db.Column(_enum(JOB_STATUSES, 'job_status'), nullable=False, default='queued')
    failure_stage = db.Column(_enum(PHASE_FAILURE_STAGES, 'phase_failure_stage'))
    error_summary = db.Column(db.String(500))

    #: The queue has nothing else to order by -- `started_at` is null until
    #: dispatch (§5).
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime)
    #: Flask *reads* this and renders "stalled". The sweep lives in the sibling
    #: so it cannot fire against a healthy run, which means a sibling that dies
    #: and stays dead is swept by nobody (§7.3).
    heartbeat_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    #: A UUID minted per sibling start, never a PID: a PID is reused across
    #: container restarts and meaningless across PID namespaces (§7.3).
    runner_instance_id = db.Column(db.String(36))
    log_path = db.Column(db.String(255))


class UpgradeHostPhaseResult(db.Model):
    """One host's outcome in one phase execution -- the record of what
    happened, as against `UpgradeRunHost.state`, which is a cursor.
    """

    __tablename__ = 'upgrade_host_phase_results'
    __table_args__ = (
        db.ForeignKeyConstraint(
            ['run_id', 'phase', 'attempt'],
            ['upgrade_phase_jobs.run_id', 'upgrade_phase_jobs.phase',
             'upgrade_phase_jobs.attempt'],
            name='fk_result_phase_job',
        ),
        db.ForeignKeyConstraint(
            ['run_id', 'hostname'],
            ['upgrade_run_hosts.run_id', 'upgrade_run_hosts.hostname'],
            name='fk_result_run_host',
        ),
    )

    run_id = db.Column(db.Integer, primary_key=True)
    hostname = db.Column(db.String(255), primary_key=True)
    phase = db.Column(_enum(PHASES, 'result_phase'), primary_key=True)
    attempt = db.Column(db.Integer, primary_key=True)

    status = db.Column(db.String(40), nullable=False)
    failure_stage = db.Column(_enum(PHASE_FAILURE_STAGES, 'result_failure_stage'))
    error_summary = db.Column(db.String(500))
    #: Set only on the stage row of a *push* run -- the only phase of the only
    #: transport that touches the device's SCP server. Null means "nothing was
    #: ever changed" under pull and "no bracket ran" otherwise, never "a change
    #: may be outstanding". Read it together with
    #: UpgradeRun.image_transport_used, never alone (§5).
    scp_restore_confirmed = db.Column(db.Boolean)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)


# A row in a terminal status doesn't get written to again, and that needs a
# trigger behind it rather than a habit (§7.3): without one, nothing separates
# "this row has always described what happened" from "this row was edited
# afterwards", which is the whole point of an audit row.
_TERMINAL_LIST = ", ".join(f"'{s}'" for s in sorted(TERMINAL_JOB_STATUSES))
db.event.listen(
    UpgradePhaseJob.__table__,
    'after_create',
    db.DDL(
        f"""
        CREATE TRIGGER upgrade_phase_jobs_terminal_immutable
        BEFORE UPDATE ON upgrade_phase_jobs
        FOR EACH ROW WHEN OLD.status IN ({_TERMINAL_LIST})
        BEGIN
            SELECT RAISE(ABORT,
                'upgrade_phase_jobs row is terminal and must not be updated');
        END;
        """
    ),
)
