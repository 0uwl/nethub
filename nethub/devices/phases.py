"""Running one phase of an upgrade across a run's hosts, and recording it.

This is the layer between the device modules and the database. It owns the
per-host loop, the exception-to-`failure_stage` mapping (§7.3), and the rows:
one `upgrade_host_phase_results` per host per execution, plus the
`upgrade_run_hosts.state` cursor and the phase job's terminal status.

It does **not** own dispatch. The sibling claims a `queued` job, sets it
`running`, and calls `execute_phase`; the startup sweep and the queue are the
sibling's too (§7.3, §9). Everything here therefore assumes it is already
running inside the sibling process -- never inside Flask, which holds the
unauthenticated route.

The credential arrives as an argument and is never written anywhere: not to a
row, not to `error_summary`, not to a log. `_summarise` is what enforces the
last of those, and it is deliberately conservative about exceptions this
codebase did not raise itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nethub.devices import connection, facts, install, transfer
from nethub.extensions import db
from nethub.models import (
    DeviceHostKey,
    UpgradeHostPhaseResult,
    UpgradePhaseJob,
    UpgradeRunHost,
)

#: Per-host cursor value after a phase succeeds. Cleanup does not advance it:
#: a cleaned host is still `verified` (§7.3 -- this column is a cursor, and
#: the per-phase detail lives in the results table).
_STATE_AFTER = {
    'precheck': 'precheck_ok',
    'stage': 'staged',
    'activate': 'activated',
    'verify': 'verified',
    'cleanup': 'verified',
}

#: `error_summary` is retained for a year (§7.4), so only exceptions this
#: codebase raised itself get their message copied into it. Anything else
#: contributes its type and nothing more -- a stray `str(exc)` from a library
#: is a durable credential leak with no other symptom (§7.3).
_OUR_EXCEPTIONS = (
    connection.DeviceConnectionError,
    transfer.TransferError,
    install.InstallError,
    facts.FactsError,
)


class UnconfirmedHost(Exception):
    """No confirmed `device_host_keys` row for this address (§4.3)."""


@dataclass(frozen=True)
class HostOutcome:
    hostname: str
    status: str
    failure_stage: str | None = None
    error_summary: str | None = None
    scp_restore_confirmed: bool | None = None
    version_pre: str | None = None
    version_post: str | None = None
    config_backup: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure_stage is None


@dataclass
class PhaseContext:
    """What an execution needs that the run row does not carry.

    The credential lives here and nowhere else: held for the life of *this
    phase execution* only (design doc §9.1 -- not the life of the run, which
    can park at a gate for days), and never copied to a row or a log.

    `search_dir` and `pull_target` come from deployment settings rather than
    from the run, but are read once at dispatch so a settings change mid-run
    cannot re-point an execution halfway through.
    """

    device_username: str
    #: repr=False: see the note on `credential_socket._Held`. This object is
    #: the credential's entire lifetime container, so its repr is the most
    #: likely accidental leak in the codebase.
    device_password: str = field(repr=False)
    search_dir: str
    pull_target: transfer.PullTarget | None = None
    reload_wait: install.ReloadWait = install.DEFAULT_RELOAD_WAIT
    #: Injectable so tests need no device and the sibling can pass its own.
    connect: Callable[..., object] | None = None

    def open(self, host: UpgradeRunHost):
        opener = self.connect or default_connect
        return opener(host, self.device_username, self.device_password)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for values written aware.

    Every comparison against a stored timestamp has to go through this. Without
    it the sibling raises `TypeError: can't compare offset-naive and
    offset-aware datetimes` the first time it checks a deadline that was read
    from the database rather than one it just built -- which is to say, always
    in production and never in a test that skips the round trip.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def failure_stage_for(exc: BaseException) -> str:
    """Map an exception to §7.3's phase-job vocabulary.

    Ordered most specific first. The point of the mapping living here rather
    than in each device module is that the modules stay usable without the
    database, and this is the only place that has to know the enum.
    """
    if isinstance(exc, (connection.HostKeyError, UnconfirmedHost)):
        return 'hostkey'
    if isinstance(exc, connection.AuthenticationError):
        return 'credential'
    if isinstance(exc, connection.DeviceConnectionError):
        return 'connect'
    if isinstance(exc, transfer.VerificationError):
        return 'checksum'
    if isinstance(exc, transfer.TransferError):
        return 'transfer'
    if isinstance(exc, install.ReloadTimeout):
        return 'reload'
    if isinstance(exc, install.PostCheckError):
        return 'postcheck'
    if isinstance(exc, install.InstallError):
        # `privilege` is worth keeping distinct: it means the submitter's
        # account cannot reach level 15, which is a deployment fault rather
        # than a device fault (§7.3).
        return 'privilege' if exc.status == 'privilege' else 'install'
    if isinstance(exc, facts.FactsError):
        return 'precheck'
    return 'connect'


def _summarise(exc: BaseException) -> str:
    if isinstance(exc, _OUR_EXCEPTIONS + (UnconfirmedHost,)):
        text = str(exc)
    else:
        text = f"unexpected {type(exc).__name__}"
    return " ".join(text.split())[:500]


def pinned_key(host: UpgradeRunHost) -> connection.HostKey:
    """The confirmed pin for this host's address, or refuse.

    An address with no already-confirmed row cannot be named by a run at all
    (§4.3), so an unconfirmed row is treated exactly like a missing one --
    seeing a key is not accepting it.
    """
    row = DeviceHostKey.query.filter_by(ansible_host=host.ansible_host).first()
    if row is None:
        raise UnconfirmedHost(
            f"{host.ansible_host} has no confirmed host key; an admin must "
            f"confirm the fingerprint before a run may target it"
        )
    if not row.is_confirmed:
        raise UnconfirmedHost(
            f"{host.ansible_host} has a host key on file that nobody has "
            f"confirmed; seeing a key is not accepting it"
        )
    return connection.HostKey(row.key_type, row.fingerprint_sha256)


def default_connect(host: UpgradeRunHost, username: str, password: str):
    return connection.connect(host.ansible_host, username, password, pinned_key(host))


# --------------------------------------------------------------------------
# The five phases. Each takes an open connection and returns a HostOutcome;
# raising is also fine -- run_host turns either into a row.
# --------------------------------------------------------------------------

def phase_precheck(conn, host: UpgradeRunHost, ctx: PhaseContext) -> HostOutcome:
    """Read-only. Runs on submit with no gate, so it must change nothing."""
    device = facts.get_facts(conn)
    privilege = facts.get_privilege(conn)
    if privilege < 15:
        raise install.InstallError(
            f"account is at privilege {privilege}, not 15", status='privilege'
        )
    if device.boot_mode != 'INSTALL':
        raise install.InstallError(
            f"boot mode is {device.boot_mode or 'unknown'}, not INSTALL",
            status='wrong_boot_mode',
        )
    filesystem = facts.get_filesystem(conn, host.flash_dir)
    already = filesystem.size_of(host.filename) or 0
    if filesystem.free_bytes + already <= host.file_size:
        raise transfer.TransferError(
            f"{host.flash_dir} has {filesystem.free_bytes} bytes free, "
            f"{host.filename} needs {host.file_size}",
            status='no_space',
        )
    return HostOutcome(host.hostname, 'precheck_ok', version_pre=device.version)


def phase_stage(conn, host: UpgradeRunHost, ctx: PhaseContext) -> HostOutcome:
    run = host.run
    outcome = transfer.stage_image(
        conn,
        image=host.filename,
        sha512=host.sha512,
        search_dir=ctx.search_dir,
        transport=run.image_transport_used,
        file_size=host.file_size,
        file_system=host.flash_dir,
        pull_target=ctx.pull_target,
    )
    return HostOutcome(
        host.hostname, outcome.status,
        scp_restore_confirmed=outcome.scp_restore_confirmed,
    )


def phase_activate(conn, host: UpgradeRunHost, ctx: PhaseContext) -> HostOutcome:
    """Reload, then wait for the device back. Verification is its own phase.

    The reconnect belongs here rather than to `verify` because `verify` cannot
    start without a device to talk to, and because a device that never returns
    is a *reload* failure, which is a different `failure_stage` and a different
    conversation than a wrong version.
    """
    backup = install.capture_running_config(conn)
    result = install.activate(
        conn,
        image=host.filename,
        sha512=host.sha512,
        target_version=host.version,
        file_system=host.flash_dir,
    )
    returned = install.wait_for_device(lambda: ctx.open(host), wait=ctx.reload_wait)
    returned.disconnect()
    return HostOutcome(
        host.hostname, 'activated',
        version_pre=result.version_before,
        config_backup=backup,
    )


def phase_verify(conn, host: UpgradeRunHost, ctx: PhaseContext) -> HostOutcome:
    device = install.verify_upgrade(conn, target_version=host.version)
    return HostOutcome(host.hostname, 'verified', version_post=device.version)


def phase_cleanup(conn, host: UpgradeRunHost, ctx: PhaseContext) -> HostOutcome:
    result = install.cleanup(conn)
    return HostOutcome(host.hostname, f"removed {len(result.removed)} files")


PHASE_RUNNERS: dict[str, Callable] = {
    'precheck': phase_precheck,
    'stage': phase_stage,
    'activate': phase_activate,
    'verify': phase_verify,
    'cleanup': phase_cleanup,
}


# --------------------------------------------------------------------------
# The driver. Called by the sibling with a job it has already claimed.
# --------------------------------------------------------------------------

def run_host(host: UpgradeRunHost, job: UpgradePhaseJob, ctx: PhaseContext) -> HostOutcome:
    """One host, one phase. Turns success or any exception into an outcome.

    Never raises: a host that fails is a row, not an aborted execution -- the
    other hosts in the wave still have to be attempted and recorded.
    """
    runner = PHASE_RUNNERS[job.phase]
    conn = None
    try:
        conn = ctx.open(host)
        return runner(conn, host, ctx)
    except Exception as exc:  # noqa: BLE001 -- every failure becomes a row
        return HostOutcome(
            host.hostname,
            status=getattr(exc, 'status', 'failed'),
            failure_stage=failure_stage_for(exc),
            error_summary=_summarise(exc),
            scp_restore_confirmed=getattr(exc, 'scp_restore_confirmed', None),
        )
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001, S110 -- a dead session is already gone
                pass


def record(host: UpgradeRunHost, job: UpgradePhaseJob, outcome: HostOutcome,
           started_at: datetime) -> None:
    """Write the result row and advance the host cursor.

    The result row is the record of what happened; `UpgradeRunHost.state` is a
    cursor for the dashboard's default view and is derived from it (§5).
    """
    db.session.add(UpgradeHostPhaseResult(
        run_id=job.run_id,
        hostname=host.hostname,
        phase=job.phase,
        attempt=job.attempt,
        status=outcome.status,
        failure_stage=outcome.failure_stage,
        error_summary=outcome.error_summary,
        scp_restore_confirmed=outcome.scp_restore_confirmed,
        started_at=started_at,
        finished_at=_utcnow(),
    ))
    host.last_phase = job.phase
    host.state = _STATE_AFTER[job.phase] if outcome.ok else 'failed'
    if outcome.error_summary:
        host.error_summary = outcome.error_summary
    if outcome.version_pre:
        host.reported_version_pre = outcome.version_pre
    if outcome.version_post:
        host.reported_version_post = outcome.version_post


def execute_phase(job: UpgradePhaseJob, ctx: PhaseContext,
                  now: Callable[[], datetime] = _utcnow) -> str:
    """Run `job`'s phase across every eligible host. Returns the job's status.

    The caller has already claimed the job and set it `running`; this writes
    the terminal edge and the per-host rows, and nothing else touches the job.

    Cancel and the deadline are checked *between hosts* and not mid-host
    (§7.3): there is no safe place to stop inside an activation, and a job row
    polled more finely would still not give one.
    """
    hosts = [h for h in job.run.hosts if h.state not in ('failed', 'skipped')]
    stopped = None

    for host in hosts:
        if job.run.cancel_requested_at is not None:
            stopped = 'cancelled'
            break
        deadline = _aware(job.deadline_at)
        if deadline is not None and now() >= deadline:
            stopped = 'timed_out'
            break

        started = now()
        outcome = run_host(host, job, ctx)
        record(host, job, outcome, started)
        job.heartbeat_at = now()
        db.session.commit()

    if stopped is None:
        failed = any(h.state == 'failed' for h in hosts)
        stopped = 'failed' if failed else 'succeeded'
        if failed:
            first = next(h for h in hosts if h.state == 'failed')
            job.failure_stage = _last_failure_stage(job, first.hostname)
            job.error_summary = first.error_summary

    job.status = stopped
    job.finished_at = now()
    db.session.commit()
    return stopped


def _last_failure_stage(job: UpgradePhaseJob, hostname: str) -> str | None:
    row = UpgradeHostPhaseResult.query.filter_by(
        run_id=job.run_id, hostname=hostname, phase=job.phase, attempt=job.attempt
    ).first()
    return row.failure_stage if row else None
