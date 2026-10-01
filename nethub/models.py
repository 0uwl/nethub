from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def _enum(values, name):
    # native_enum=False keeps this a VARCHAR plus a CHECK constraint, which is
    # what SQLite can actually enforce -- and `create_constraint=True` is not
    # optional decoration: SQLAlchemy has defaulted it to False since 1.4, so
    # without it these columns are plain strings and every vocabulary below is
    # documentation rather than a constraint. Verified by reading the emitted
    # DDL, not by assuming.
    return db.Enum(*values, name=name, native_enum=False, create_constraint=True)


#: `user.role` (PLAN.md WS-16). An operator does the day-to-day work: artifacts,
#: host keys and runs. An admin does that and also manages users and NetHub's
#: settings, and is exempt from the two-person rules.
ROLES = ('admin', 'operator')


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
    #: Least privilege unless something says otherwise: `create-admin` and
    #: first-boot bootstrap set `admin`, and the migration that added the
    #: column made every user who existed then an admin, which they all were.
    #: Read from the row on every request (`load_user`), so a change applies on
    #: the next click.
    role = db.Column(_enum(ROLES, 'user_role'), nullable=False, default='operator',
                     server_default='operator')

    #: Deactivation rather than deletion (design doc §5): the audit trail
    #: references these rows. An inactive user cannot log in, and the user
    #: loader refuses them, so an existing session ends on its next request
    #: (PLAN.md WS-10).
    is_active = db.Column(db.Boolean, nullable=False, default=True, server_default='1')
    #: Bumped to revoke every session this user holds: on a password change,
    #: an admin reset and a disable. It is part of the id Flask-Login keeps in
    #: the signed cookie (`get_id`), so a cookie minted under an older epoch no
    #: longer loads a user. That is revocation without the `sessions` table
    #: §4.5 specifies, which alpha does not have.
    session_epoch = db.Column(db.Integer, nullable=False, default=0, server_default='0')

    # Online-guessing budget. Deliberately per-*user* rather than per-submitted
    # -username: design doc §4.2 argues at length that a counter keyed on
    # attacker-chosen input is an unbounded-growth attack on the SQLite file
    # everything else shares, and a username is attacker-chosen. Keying on the
    # row bounds the key space to the users table. Attempts against usernames
    # that do not exist are therefore NOT counted -- which is safe only because
    # the timing oracle that made them enumerable is closed in auth.py, so an
    # attacker cannot learn which usernames are worth spending a budget on.
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)

    @property
    def is_admin(self):
        return self.role == 'admin'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def get_id(self):
        # Flask-Login stores this in the session cookie and hands it back to
        # `load_user`; carrying the epoch here is what makes a bump revoke.
        return f'{self.id}:{self.session_epoch}'

    def revoke_sessions(self):
        self.session_epoch = (self.session_epoch or 0) + 1

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_locked(self, now=None):
        if self.locked_until is None:
            return False
        now = now or datetime.now(timezone.utc)
        # SQLite hands back naive datetimes for values written aware, so a
        # round-tripped `locked_until` would raise TypeError against an aware
        # `now` -- the same trap phases._aware()/sibling._aware() exist for.
        locked_until = self.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        return locked_until > now

    def register_failed_login(self, *, limit, lockout, now=None):
        """Count one failure and lock the account once it reaches `limit`."""
        now = now or datetime.now(timezone.utc)
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= limit:
            self.locked_until = now + lockout
            self.failed_logins = 0
        return self.is_locked(now)

    def clear_failed_logins(self):
        self.failed_logins = 0
        self.locked_until = None


def _utcnow():
    return datetime.now(timezone.utc)


#: §5's artifact vocabulary. An upload lands `staged` when the two-person rule
#: for artifacts applies to its uploader, and someone else publishes it
#: (PLAN.md WS-16); otherwise it lands `published`. Nothing writes
#: `superseded`: there is no supersede flow, matching the no-supersede stance
#: the YAML store had. It exists because §7.4's retention story references it.
ARTIFACT_STATES = ('staged', 'published', 'superseded')
ARTIFACT_KINDS = ('script', 'config', 'image')
#: §7.4 splits blob retention from row retention: the row outlives the bytes
#: and says so, rather than leaving a path that silently stops resolving.
BYTES_STATES = ('present', 'pruned')


class Artifact(db.Model):
    """The single ingest record behind both days (design doc §3.4, §5).

    Every byte NetHub serves has exactly one row here. This replaced the
    `software_registry` YAML store and its `Registry` pointer rows at build
    step 7 -- that block existed so an Ansible playbook could read it, and
    there is no playbook.
    """

    __tablename__ = 'artifacts'
    __table_args__ = (
        # Load-bearing rather than tidy (§5). The push addresses the
        # source by *filename* under one directory, so without this two
        # uploads sharing an original filename promote to the same path and
        # silently overwrite each other's bytes -- and every downstream hash
        # check still passes, because each compares a row's own sha512 against
        # whatever currently sits at that path. That would quietly break the
        # "hashed once, consumed three times" chain of custody §3.4 is built on.
        db.Index('uq_artifact_filename_live', 'filename', unique=True,
                 sqlite_where=db.text("state IN ('staged', 'published')")),
        # One published image per bundle key per platform, enforced by the
        # database rather than by whatever writes the row remembering to check.
        db.Index('uq_artifact_bundle_key', 'platform', 'bundle_key', unique=True,
                 sqlite_where=db.text("kind = 'image' AND state = 'published'")),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(_enum(ARTIFACT_KINDS, 'artifact_kind'), nullable=False, default='image')
    platform = db.Column(db.String(32), nullable=False, default='iosxe')
    #: The key a request names to select this artifact. What made the registry
    #: renderable, and what a submitted request resolves against.
    bundle_key = db.Column(db.String(80), nullable=False)

    filename = db.Column(db.String(255), nullable=False)
    sha512 = db.Column(db.String(128), nullable=False)
    file_size = db.Column(db.BigInteger, nullable=False)
    #: Where the blob actually lives, and what a retention purge collects by.
    #: There is no `remote_dir`: the push addresses one deployment-wide
    #: directory by filename (§5).
    storage_path = db.Column(db.String(500), nullable=False)
    version = db.Column(db.String(32), nullable=False)

    state = db.Column(_enum(ARTIFACT_STATES, 'artifact_state'),
                      nullable=False, default='published')
    superseded_by_id = db.Column(db.Integer, db.ForeignKey('artifacts.id'))
    bytes_state = db.Column(_enum(BYTES_STATES, 'artifact_bytes_state'),
                            nullable=False, default='present')
    bytes_pruned_at = db.Column(db.DateTime)

    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    #: Who made it nameable by a run. The uploader, unless the two-person rule
    #: held it `staged` for someone else (PLAN.md WS-16).
    published_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    published_at = db.Column(db.DateTime)
    #: A delete waiting for a second person. The artifact stays usable until
    #: someone else confirms it.
    delete_requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    delete_requested_at = db.Column(db.DateTime)


#: `artifact_audit.action` (PLAN.md WS-16).
ARTIFACT_AUDIT_ACTIONS = ('uploaded', 'published', 'withdrawn', 'delete_requested', 'deleted')


class ArtifactAudit(db.Model):
    """Who uploaded, published and deleted which image, with the role they
    held at the time (PLAN.md WS-16).

    Keyed on copies of the artifact's identity rather than a foreign key, so
    it outlives the row a delete removes: an admin deleting alone has to stay
    visible after the thing deleted is gone. Append-only by trigger.
    """

    __tablename__ = 'artifact_audit'

    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    artifact_id = db.Column(db.Integer, nullable=False)
    bundle_key = db.Column(db.String(80), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    sha512 = db.Column(db.String(128), nullable=False)
    action = db.Column(_enum(ARTIFACT_AUDIT_ACTIONS, 'artifact_audit_action'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    actor_role = db.Column(_enum(ROLES, 'artifact_audit_actor_role'))


def record_artifact_action(action, artifact, actor):
    """Add an `artifact_audit` row; the caller commits it with the change."""
    db.session.add(ArtifactAudit(
        action=action, artifact_id=artifact.id, bundle_key=artifact.bundle_key,
        filename=artifact.filename, sha512=artifact.sha512,
        actor_id=actor.id if actor is not None else None,
        actor_role=actor.role if actor is not None else None,
    ))


@login_manager.user_loader
def load_user(user_id):
    """The user a session cookie names, or None to log it out.

    The cookie carries `id:epoch` (`User.get_id`). A bare id, from a cookie
    issued before sessions carried an epoch, is refused: everyone logs in
    again once after the upgrade that added it. A disabled user is refused
    too, so disabling someone ends their session on its next request.
    """
    user_part, sep, epoch_part = str(user_id).partition(':')
    if not sep or not user_part.isdigit() or not epoch_part.isdigit():
        return None
    user = db.session.get(User, int(user_part))
    if user is None or not user.is_active or user.session_epoch != int(epoch_part):
        return None
    return user


#: `user_admin_audit.action` (PLAN.md WS-10). `created` covers the web form,
#: `create-admin` and first-boot bootstrap alike.
USER_AUDIT_ACTIONS = (
    'created', 'password_changed', 'password_reset', 'disabled', 'enabled', 'unlocked',
    'role_changed',
)


class UserAdminAudit(db.Model):
    """Who did what to which account, and when (design doc §5).

    Append-only by trigger, not by habit: a Flask-side compromise that can
    forge a user could otherwise also erase the record of having done it.
    `actor_user_id` is null for the command line and first-boot bootstrap,
    which act with host access rather than as a NetHub user.
    """

    __tablename__ = 'user_admin_audit'

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(_enum(USER_AUDIT_ACTIONS, 'user_audit_action'), nullable=False)
    #: Short, fixed text of ours: never a password, never request input.
    detail = db.Column(db.String(200))


def record_user_action(action, target, actor=None, detail=None):
    """Add a `user_admin_audit` row to the session; the caller commits it
    with the change it records, so neither lands without the other."""
    db.session.add(UserAdminAudit(
        action=action, target_user_id=target.id,
        actor_user_id=actor.id if actor is not None else None, detail=detail,
    ))


def _append_only(table):
    """`BEFORE UPDATE` and `BEFORE DELETE` triggers that always abort: an audit
    table a Flask-side compromise could rewrite would not be an audit table
    (design doc §5)."""
    for event in ('UPDATE', 'DELETE'):
        db.event.listen(
            table,
            'after_create',
            db.DDL(
                f"""
                CREATE TRIGGER {table.name}_no_{event.lower()}
                BEFORE {event} ON {table.name}
                BEGIN
                    SELECT RAISE(ABORT, '{table.name} is append-only');
                END;
                """
            ),
        )


_append_only(UserAdminAudit.__table__)
_append_only(ArtifactAudit.__table__)


#: The settings an admin changes on the settings page (PLAN.md WS-16), each
#: off unless a row says otherwise. A two-person rule applies to operators
#: only: an admin acts alone, and the audit rows record that it was an admin.
TWO_PERSON_RULES = ('two_person_artifacts', 'two_person_hostkeys', 'two_person_runs')


class Setting(db.Model):
    """A deployment setting (design doc §5). Read on every request and never
    cached, so a change applies to the next action. Only the two-person rules
    live here so far; the env-var settings (`DEVICE_TARGET_CIDRS` and the
    rest) have not moved."""

    __tablename__ = 'settings'

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


class SettingsAudit(db.Model):
    """Every settings change with its old and new value (design doc §5).
    Append-only by trigger and never purged."""

    __tablename__ = 'settings_audit'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), nullable=False)
    old_value = db.Column(db.String(255))
    new_value = db.Column(db.String(255), nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    changed_at = db.Column(db.DateTime, nullable=False, default=_utcnow)


_append_only(SettingsAudit.__table__)


# ---------------------------------------------------------------------------
# Software Lifecycle: upgrade runs, their phases, and the host keys a run may
# target. Design doc §5 for the columns, §7.3 for the three state machines and
# who writes each edge, §8.1 for the phase model itself.
#
# Two deviations from §5's column lists, both because the Ansible-era
# execution environment is gone: there is no `private_data_dir` -- nothing renders a directory
# for an execution to read -- and `playbook_log_path` is `log_path`, since
# what it points at is our own transcript rather than a playbook's.
# ---------------------------------------------------------------------------

#: §8.1's phases, in order. `cleanup` is optional; declining it closes the run.
PHASES = ('precheck', 'stage', 'activate', 'verify', 'cleanup')

#: The phases §8.1 puts an approval gate in front of. Lives here with the other
#: vocabularies rather than in `upgrades.py`, because the sibling needs it too:
#: `sweep()` parks an abandoned phase at its gate only if a human can actually
#: approve it, and importing `upgrades` into the sibling would drag the artifact
#: store along with it. `precheck` is absent by design (it runs on submit, with
#: no gate) and so is `verify` (it follows `activate` without one).
APPROVABLE = ('stage', 'activate', 'cleanup')

#: §7.3 job status, shared by every job kind so one sweep serves them all.
#: `succeeded` means every host the phase ran on, `failed` means none of them,
#: and `partial` is the rest (PLAN.md WS-8): the run carries on with the hosts
#: that passed, and the failed ones can be retried. Changing this tuple
#: changes a CHECK constraint and the terminal-status trigger, so it needs a
#: migration (see 0004).
JOB_STATUSES = (
    'queued', 'running',
    'succeeded', 'partial', 'failed', 'timed_out', 'abandoned', 'cancelled', 'expired',
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

#: The cursor a host must be at for a phase to run on it, and the one a retry
#: puts a failed host back to (PLAN.md WS-8). Cleanup does not advance the
#: cursor, so it runs on, and leaves hosts at, `verified`.
STATE_BEFORE = {
    'precheck': 'pending',
    'stage': 'precheck_ok',
    'activate': 'staged',
    'verify': 'activated',
    'cleanup': 'verified',
}

#: Which phases a retry may name while the run waits at a gate: the ones that
#: ran since the previous gate (PLAN.md WS-8). A retry then carries on exactly
#: as the phase did the first time and lands back at the same gate. Nothing
#: follows cleanup, so there is no gate at which to retry it.
RETRYABLE_AT = {
    'stage': ('precheck',),
    'activate': ('stage',),
    'cleanup': ('activate', 'verify'),
}

#: §7.3 failure_stage, phase-job vocabulary. Deliberately *not* shared with the
#: publish side: a publish stops at promote/render/commit, and §8.1's phases are
#: themselves named `stage` and `verify`, so one vocabulary would produce a row
#: reading phase='activate', failure_stage='stage' that is ambiguous on its face.
#: `internal` is an error in NetHub's own code (`Sibling.recover_own`, and any
#: exception `phases.failure_stage_for` does not recognise), which used to be
#: recorded as `connect` and read as a device problem. `store` is the image in
#: NetHub's own artifact store missing or not matching its recorded digest,
#: found before a stage touches any device (PLAN.md WS-15). Changing this
#: tuple changes a CHECK constraint, so it needs a migration (see 0002, 0006).
PHASE_FAILURE_STAGES = (
    'credential', 'connect', 'hostkey', 'privilege', 'precheck',
    'transfer', 'checksum', 'install', 'reload', 'postcheck', 'internal', 'store',
)


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
    #: A removal waiting for a second person (PLAN.md WS-16). The pin keeps
    #: working until someone else confirms it.
    delete_requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    delete_requested_at = db.Column(db.DateTime)

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None


#: `HostKeyScan.status` -- a deliberately smaller subset of `JOB_STATUSES`
#: (WS-6.2b). A scan has no approval gate and nothing to time out against
#: beyond `connection.CONNECT_TIMEOUT`, so `cancelled`/`expired`/`timed_out`
#: don't apply the way they do to a phase job.
HOSTKEY_SCAN_STATUSES = ('queued', 'running', 'succeeded', 'failed', 'abandoned')

#: `DeviceHostKeyAudit.action` (WS-6.4, WS-16).
HOSTKEY_AUDIT_ACTIONS = ('confirmed', 'delete_requested', 'deleted')


class HostKeyScan(db.Model):
    """One scan of an address's host key, dispatched to the sibling (WS-6.2b).

    Scanning is device I/O -- a TCP connect and an SSH key exchange -- so it
    belongs in the process that does all other device I/O; §3.2's argument
    against blocking Flask applies to any blocking device call, not only
    phase executions. It needs no device credential (the key is exchanged
    before authentication), so nothing is sealed for it -- it is
    dispatched like a phase job, just simpler: no state machine, no
    approval gate, no per-host loop.

    `ansible_host` is a plain string, not a foreign key to `DeviceHostKey` --
    this row's lifecycle is independent of whatever pin exists (or does not)
    at that address, and `confirm_hostkey` (WS-6.3) has to bind against a
    scan even when no `DeviceHostKey` row exists there yet.
    """

    __tablename__ = 'host_key_scans'

    id = db.Column(db.Integer, primary_key=True)
    ansible_host = db.Column(db.String(64), nullable=False)
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    status = db.Column(_enum(HOSTKEY_SCAN_STATUSES, 'hostkey_scan_status'),
                       nullable=False, default='queued')
    key_type = db.Column(db.String(32))
    fingerprint_sha256 = db.Column(db.String(64))
    error_summary = db.Column(db.String(500))

    #: The queue has nothing else to order by -- `started_at` is null until
    #: claimed, mirroring `UpgradePhaseJob` (§5).
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    #: Same reason `UpgradePhaseJob` has one: lets `sweep()` abandon a scan
    #: stranded by a dead sibling instance, via the same NULL-safe predicate
    #: WS-3.4 fixed for phase jobs.
    runner_instance_id = db.Column(db.String(36))

    #: Set once this scan has backed a confirmation (WS-6.3). A succeeded scan
    #: may confirm at most once, the same one-shot pattern §4.1 uses for the
    #: provisioning allowlist -- without it, one scan could back two different
    #: confirmations later, reopening the gap WS-6.3 exists to close.
    consumed_at = db.Column(db.DateTime)


class DeviceHostKeyAudit(db.Model):
    """Who confirmed or deleted a pin, and what it was pinned to (WS-6.4).

    `ansible_host` is a plain string, **not** a foreign key to
    `device_host_keys.id` -- the whole point of this table is that it
    outlives a deleted row (no cascade to break, nothing to restore). The
    `key_type`/`fingerprint_sha256` pair is the pre-image: the value being
    recorded, for `confirmed`, or removed, for `deleted` -- the part that
    actually answers "what was this pinned to before".
    """

    __tablename__ = 'device_host_key_audit'

    id = db.Column(db.Integer, primary_key=True)
    ansible_host = db.Column(db.String(64), nullable=False)
    action = db.Column(_enum(HOSTKEY_AUDIT_ACTIONS, 'hostkey_audit_action'), nullable=False)
    key_type = db.Column(db.String(32), nullable=False)
    fingerprint_sha256 = db.Column(db.String(64), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    #: The other person (PLAN.md WS-16): who requested the scan a
    #: confirmation used, or who requested the removal a delete carried out.
    #: The same as `actor_id` when one person did both.
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    #: The actor's role when they acted, since a role can change later.
    #: Null on rows from before roles existed, when everyone was an admin.
    actor_role = db.Column(_enum(ROLES, 'hostkey_audit_actor_role'))


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

    #: In request order (`UpgradeRunHost.position`), which is what makes the
    #: first host listed the activate canary (PLAN.md WS-15).
    hosts = db.relationship('UpgradeRunHost', backref='run', cascade='all, delete-orphan',
                            order_by='UpgradeRunHost.position')
    phase_jobs = db.relationship('UpgradePhaseJob', backref='run', cascade='all, delete-orphan')


class UpgradeRunHost(db.Model):
    """One targeted device, with the bundle it was targeted at snapshotted.

    `file_size` is here so a phase can render what it needs from the run's own
    rows: re-reading it from the artifact would let a mid-run supersede
    silently re-target the run (§5).
    """

    __tablename__ = 'upgrade_run_hosts'
    __table_args__ = (
        db.UniqueConstraint('run_id', 'position', name='uq_run_host_position'),
    )

    run_id = db.Column(db.Integer, db.ForeignKey('upgrade_runs.id'), primary_key=True)
    hostname = db.Column(db.String(255), primary_key=True)
    #: The host's line in the request, from 0 (PLAN.md WS-15). Activate
    #: upgrades the first eligible host alone, as a canary, so the submitter's
    #: order has to survive into the rows: phases read rows, never the request
    #: document.
    position = db.Column(db.Integer, nullable=False)
    ansible_host = db.Column(db.String(64), nullable=False)

    #: The artifact this host's snapshot was taken from. `SET NULL` rather
    #: than `RESTRICT`: a finished run keeps its own copy of everything it
    #: needed (the columns below), so deleting the artifact afterwards costs
    #: the run only this link. `artifacts.delete()` refuses while a run that
    #: can still stage or activate references the row. The key also makes a
    #: submit that races a delete fail at insert instead of pointing at a row
    #: that is gone.
    artifact_id = db.Column(db.Integer, db.ForeignKey('artifacts.id', ondelete='SET NULL'))
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
        # Ciphertext exists only while the job waits to be claimed (PLAN.md
        # WS-7). The claim clears it in the same statement, and every path
        # that ends a job before a claim clears it too; this is the database
        # holding every writer to that, not a habit.
        db.CheckConstraint("status = 'queued' OR sealed_credential IS NULL",
                           name='ck_sealed_credential_only_while_queued'),
        db.CheckConstraint('concurrency IS NULL OR concurrency >= 1',
                           name='ck_phase_job_concurrency_positive'),
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
    #: The approver's role when they approved (PLAN.md WS-16), so an admin
    #: approving their own run under the two-person rule stays visible after
    #: their role changes.
    approved_by_role = db.Column(_enum(ROLES, 'job_approved_by_role'))
    #: The device username the device saw for this phase: the supplier's, as
    #: `_seal_into` sealed it (PLAN.md WS-16). `upgrade_runs.device_username_used`
    #: is the submitter's; when someone else approves a gate, the two differ,
    #: and this is the one the device's AAA log agrees with. `verify` carries
    #: `activate`'s, since it runs on that credential. Null on jobs from
    #: before it existed.
    device_username_used = db.Column(db.String(80))

    status = db.Column(_enum(JOB_STATUSES, 'job_status'), nullable=False, default='queued')
    #: A retry of this phase on the hosts that failed it (PLAN.md WS-8). The
    #: sibling puts those hosts' cursors back when it starts the job, because
    #: §7.3 gives every per-host edge to the sibling and none to Flask.
    is_retry = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    #: How many devices an activate reloads at once after its canary, chosen
    #: by the approver (PLAN.md WS-15) and kept here so the audit trail shows
    #: who chose to reload several together. The sibling caps it at its
    #: `PHASE_CONCURRENCY`. Null on every other phase, which run at the cap.
    concurrency = db.Column(db.Integer)
    failure_stage = db.Column(_enum(PHASE_FAILURE_STAGES, 'phase_failure_stage'))
    error_summary = db.Column(db.String(500))

    #: The queue has nothing else to order by -- `started_at` is null until
    #: dispatch (§5) -- except a start time an approver chose (`not_before`);
    #: `due_at()` is the coalesce of the two that the queue actually uses.
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    #: The earliest the sibling may claim this job, UTC, or null for "now"
    #: (PLAN.md WS-14). An approval for a maintenance window is still an
    #: ordinary approval: the credential is sealed into the row as usual and
    #: `deadline_at` is measured from here, so the sealed `expires_at`
    #: follows the window rather than needing a TTL of its own.
    not_before = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    #: Flask *reads* this and renders "stalled" (`worker_status`). The sweep lives in the sibling
    #: so it cannot fire against a healthy run, which means a sibling that dies
    #: and stays dead is swept by nobody (§7.3).
    heartbeat_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    #: A UUID minted per sibling start, never a PID: a PID is reused across
    #: container restarts and meaningless across PID namespaces (§7.3).
    runner_instance_id = db.Column(db.String(36))
    log_path = db.Column(db.String(255))

    #: The device credential that approval supplied, sealed to the sibling's
    #: public key (nethub/sealed_credentials.py): Flask can write it and never
    #: read it. Null for `verify`, which runs on `activate`'s credential, and
    #: for every job that is no longer queued.
    sealed_credential = db.Column(db.LargeBinary)


def due_at():
    """When a queued job becomes claimable: its `not_before`, or the moment it
    was created (PLAN.md WS-14).

    One expression rather than three, because the sibling's queue and the two
    things the web pages infer from it -- "nothing is picking up work" and
    "queued ahead of you" -- have to agree on what "waiting" means. A job
    scheduled for tonight is not waiting on anything.
    """
    return db.func.coalesce(UpgradePhaseJob.not_before, UpgradePhaseJob.created_at)


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
    #: Set only on a stage row -- the only phase that touches the device's SCP
    #: server. Null means "no bracket ran", never "a change may be
    #: outstanding" (§5).
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
