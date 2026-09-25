"""Compiling an upgrade request into a run, and moving it through its gates.

The routes are thin over this, the way `registry_routes.py` is thin over
`registry.py`. Everything that decides whether a run may exist lives here.

A submitter sends a *request document* -- hosts, one bundle key each, and
nothing else -- which NetHub validates and compiles into rows (design doc
§8.1). There is no inventory to upload and no template to render: the only
connection var a request may carry is the target address, and it is validated
rather than trusted. Everything that says *who someone is* is read
server-side.

**What a request may set.** This table used to live in
`ansible/inventory/README.md`, which build step 6 deleted along with the rest
of that layer; it is the contract itself rather than documentation of the
playbooks, so it moved here with the code that enforces it.

| Field | Notes |
|---|---|
| `platform` | Must be supported. `iosxe` only today, and currently implicit. |
| `hosts[].name` | The inventory hostname, unique within a run. |
| `hosts[].ansible_host` | Address. An IP literal inside a configured CIDR. |
| `hosts[].bundle` | A bare registry key. Resolved server-side. |
| `hosts[].flash_dir` | Optional. `flash:` / `bootflash:`. |

Anything else is rejected. Connection vars, credentials and registry entries
are NetHub's to write. Beyond this set the answer is a pull request against
the code, not a runtime upload.

**Two deliberate narrowings against that table, both worth knowing:** this
implementation takes *one* bundle for the whole run rather than one per host,
and `flash_dir` is not submittable at all (it defaults on the column). Both
are simplifications of the contract, not disagreements with it -- widening
them is additive and needs no rule revisited.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from . import artifacts as artifact_store
from .extensions import db
from .models import (
    APPROVABLE,
    RETRYABLE_AT,
    DeviceHostKey,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
)
from .sealed_credentials import CredentialError, check_credential, seal

#: Wall-clock budget per phase, as (fixed seconds, seconds per host).
#:
#: `deadline_at` was declared on the model and read in two places
#: (`sibling.run_once`, `phases.execute_phase`) and **written by nothing** --
#: so §7.3's `timed_out` and `expired` were unreachable states and a phase
#: execution had no wall-clock bound at all.
#:
#: Be precise about what this does and does not bound. The deadline is checked
#: **between hosts**, never mid-host (§7.3 -- there is no safe place to stop
#: inside an activation), so it does *not* rescue a single wedged device: one
#: host that answers SSH and never finishes its SCP put still burns
#: `TRANSFER_READ_TIMEOUT`, which is 7200s, and that timeout is the only thing
#: bounding it. What the deadline bounds is the **wave** -- a 40-host stage
#: that would otherwise keep the sibling's single FIFO queue busy with no
#: limit of any kind.
#:
#: Derived from the timings CLAUDE.md records against real hardware, then
#: multiplied by SAFETY. A deadline that fires on a healthy run is worse than
#: no deadline, so these are deliberately loose: the point is to bound a
#: wedged phase, not to police a slow one.
#:
#:   stage     ~370s for 471 MB over SCP (~1.3 MB/s), plus two
#:             `verify /sha512` passes at ~34s per 408 MB
#:   activate  `install add ... activate commit` 605-622s, then a reload of
#:             228-238s before the CLI serves again
#:   cleanup   `install remove inactive` ~5s
#:   precheck  three reads
#:   verify    one read after the reload
PHASE_BUDGET_SECONDS = {
    'precheck': (300, 120),
    'stage': (600, 300),
    'activate': (600, 1200),
    'verify': (300, 180),
    'cleanup': (300, 120),
}

#: Multiplier on the sum above. Also covers a stack or a slower chassis, both
#: of which CLAUDE.md lists as untested.
#:
#: 2, not more: at 3 a 20-host stage budget came out at ~15 hours, which is
#: longer than the 2-hour per-host read timeout it sits above and therefore
#: not a bound anyone would notice. These are a first cut from single-device
#: measurements -- re-derive them from a real multi-host wave when there is
#: one, rather than trusting the arithmetic here.
DEADLINE_SAFETY = 2

#: Seconds per megabyte of image, added to the stage budget only. Covers the
#: transfer at a pessimistic ~1 MB/s plus the two digest passes.
STAGE_SECONDS_PER_MB = 1.3


def phase_deadline(phase, *, hosts, image_bytes=0, now=None):
    """When a phase execution stops being allowed to run.

    Scales with host count because a phase walks hosts one at a time, and --
    for `stage` only -- with image size, which is the term that actually
    dominates. Returns an aware datetime; `phases._aware()` and
    `sibling._aware()` exist because SQLite hands these back naive.
    """
    fixed, per_host = PHASE_BUDGET_SECONDS[phase]
    seconds = fixed + per_host * max(hosts, 1)
    if phase == 'stage':
        megabytes = image_bytes / (1024 * 1024)
        seconds += STAGE_SECONDS_PER_MB * megabytes * max(hosts, 1)
    return (now or _utcnow()) + timedelta(seconds=seconds * DEADLINE_SAFETY)


#: Which phases a human approves -- `precheck` runs on submit with no gate and
#: `verify` follows `activate` automatically, both read-only (§8.1). Imported
#: rather than defined here so `upgrades.APPROVABLE` keeps working for
#: `upgrade_routes`, while the sibling can reach it without importing this
#: module (and the artifact store behind it).
__all__ = ['APPROVABLE']


class RequestError(Exception):
    """The submitted request cannot be compiled into a run."""


def _aware(value):
    """SQLite hands back naive datetimes for values written aware; the same
    helper as `phases._aware`, kept here so this module needs no device
    imports."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _utcnow():
    return datetime.now(timezone.utc)


def parse_hosts(raw: str) -> list[tuple[str, str]]:
    """`hostname, address` per line. A closed shape, not an inventory."""
    hosts = []
    seen = set()
    for number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.replace('\t', ',').split(',') if p.strip()]
        if len(parts) != 2:
            raise RequestError(f'Line {number}: expected "hostname, address".')
        hostname, address = parts
        if hostname in seen:
            # PRIMARY KEY (run_id, hostname) would refuse this anyway; saying so
            # here names the line instead of surfacing an IntegrityError.
            raise RequestError(f'Line {number}: "{hostname}" is named twice.')
        seen.add(hostname)
        hosts.append((hostname, address))
    if not hosts:
        raise RequestError('No hosts given.')
    return hosts


def check_target(address: str, cidrs) -> str:
    """An address must be a literal inside a configured CIDR.

    A hostname is refused rather than resolved: the CIDR check and the eventual
    connection would resolve it at different times, and `device_host_keys`
    would end up keyed on a string whose meaning can change afterwards
    (design doc §4.3).
    """
    if not cidrs:
        raise RequestError(
            'No DEVICE_TARGET_CIDRS configured, so no address can be accepted. '
            'Set it before submitting a run.'
        )
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise RequestError(
            f'"{address}" is not an IP literal. Targets are addresses, not names.'
        ) from None
    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return str(ip)
        except ValueError:
            raise RequestError(f'Configured target CIDR "{cidr}" is not valid.') from None
    raise RequestError(f'{address} is outside the configured target CIDRs.')


def confirmed_key(address: str) -> DeviceHostKey:
    """An address with no confirmed pin cannot be named by a run at all.

    Not a TOFU prompt deferred to dispatch: pinning fails closed only on a
    *changed* key, and "an operator names a machine they control" is always a
    first contact. Confirming one is a separate admin action (§4.3).
    """
    row = DeviceHostKey.query.filter_by(ansible_host=address).first()
    if row is None or not row.is_confirmed:
        raise RequestError(
            f'{address} has no confirmed host key. An admin must confirm its '
            f'fingerprint before any run may target it.'
        )
    return row


def resolve_bundle(bundle: str, platform: str = 'iosxe'):
    """A request names a key; NetHub resolves it to a row.

    Never a filename and never a digest -- a submitted pair would name any
    bytes against any checksum and bypass the table that owns both.
    """
    try:
        return artifact_store.get_published(bundle, platform=platform)
    except artifact_store.ArtifactError as exc:
        raise RequestError(str(exc)) from None


def build_document(bundle: str, hosts) -> str:
    """The request as NetHub understood it, stored beside its digest.

    §3.4 hashes what it ingests, and a document deciding which images land on
    which devices is not the exception -- but a digest whose preimage is stored
    nowhere is unverifiable, so both go in the row.
    """
    return json.dumps(
        {
            'bundle': bundle,
            'hosts': [{'hostname': h, 'ansible_host': a} for h, a in hosts],
        },
        sort_keys=True,
        separators=(',', ':'),
    )


def _require_device_username(user):
    """Submit and approve both collect this user's device credential, so both
    need the name it goes with. Without the check on approve, the route held
    `None` as the username and the sibling refused it as a malformed
    credential, after the gate had already been spent."""
    if not user.device_username:
        raise RequestError(
            'Your device username is not set. NetHub maps it server-side and '
            'will not take it from a request -- set it on your profile first.'
        )


def _check_credential(user, password):
    """Refuse a credential the sibling would refuse, before anything is written.

    The sealing step checks both again, but by then the run's rows are
    flushed; failing here keeps a refusal a plain message with nothing to
    undo.
    """
    for what, value in (('Device username', user.device_username),
                        ('Device password', password)):
        try:
            check_credential(value)
        except CredentialError as exc:
            raise RequestError(f'{what} refused: {exc}.') from None


def _seal_into(job, *, user, password, public_key):
    """Seal the supplier's credential into the flushed job row (PLAN.md WS-7).

    Same transaction as the row's creation, so there is no moment at which the
    sibling can claim a `queued` job whose credential is not there yet -- the
    race WS-1 closed by ordering, closed here by construction. It expires with
    the job's deadline: a credential waits exactly as long as its job may.
    """
    job.sealed_credential = seal(
        public_key,
        job_id=job.id,
        approved_by=user.id,
        username=user.device_username,
        password=password,
        expires_at=job.deadline_at,
    )


def submit(*, user, bundle, hosts_raw, cidrs, password, public_key,
           flash_dir='flash:', platform='iosxe'):
    """Compile a request into a run, its host rows, and a queued pre-check.

    Flask writes exactly one job edge in the whole system and this is it: the
    row that arrives `queued` (§7.3). Everything after dispatch is the
    sibling's. Pre-check has no gate but still opens a device session, so the
    submitter's credential is sealed into it here (§8.1: "runs on submit").
    """
    _require_device_username(user)
    _check_credential(user, password)
    hosts = parse_hosts(hosts_raw)
    artifact = resolve_bundle(bundle, platform=platform)
    targets = []
    for hostname, address in hosts:
        checked = check_target(address, cidrs)
        confirmed_key(checked)
        targets.append((hostname, checked))

    run = UpgradeRun(
        submitted_by=user.id,
        device_username_used=user.device_username,
        request_document=build_document(bundle, targets),
        request_sha512='',
        state='pre_checking',
        created_at=_utcnow(),
    )
    run.request_sha512 = hashlib.sha512(run.request_document.encode()).hexdigest()
    db.session.add(run)
    db.session.flush()

    for hostname, address in targets:
        db.session.add(UpgradeRunHost(
            run_id=run.id,
            hostname=hostname,
            ansible_host=address,
            artifact_id=artifact.id,
            bundle_key=bundle,
            filename=artifact.filename,
            sha512=artifact.sha512,
            version=artifact.version,
            file_size=artifact.file_size,
            flash_dir=flash_dir,
            state='pending',
        ))
    try:
        # Flushed explicitly so the race below surfaces here rather than in
        # whatever autoflush happens to come next.
        db.session.flush()
    except IntegrityError:
        # The artifact was deleted between `resolve_bundle` above and this
        # insert, and the host rows' foreign key refuses to point at it.
        # `artifacts.delete` is one conditional statement, so this is the only
        # ordering in which that race reaches here.
        db.session.rollback()
        resolve_bundle(bundle, platform=platform)  # raises the usual refusal
        raise

    job = UpgradePhaseJob(
        run_id=run.id, phase='precheck', attempt=1, status='queued',
        created_at=_utcnow(),
        deadline_at=phase_deadline('precheck', hosts=len(run.hosts)),
    )
    db.session.add(job)
    db.session.flush()
    _seal_into(job, user=user, password=password, public_key=public_key)
    db.session.commit()
    return run, job


def approve(*, run, phase, user, password, public_key):
    """Write the phase job a gate is waiting for.

    Two admins both clicking "approve: reload" is the case this has to refuse:
    §8.1's serialization is scoped to *execution*, so it would otherwise queue
    two reloads that then run one after the other. The check below turns the
    sequential case into a message; `_queue_from_gate` catches the concurrent
    one, and `UNIQUE(run_id, phase, attempt)` holds either way.

    The approval is what supplies the credential, so it is sealed into the
    job in the same transaction (see `_seal_into`).
    """
    _check_gate(run)
    if phase not in APPROVABLE:
        raise RequestError(f'"{phase}" is not a phase anyone approves.')
    if run.awaiting_phase != phase:
        raise RequestError(
            f'This run is waiting at the {run.awaiting_phase} gate, not {phase}.'
        )
    return _queue_from_gate(run=run, phase=phase, user=user, password=password,
                            public_key=public_key, is_retry=False,
                            hosts=len(run.hosts))


def retryable_phases(run):
    """What a retry may name at the run's current gate, with the hosts it would
    run on: `[(phase, [hostname, ...]), ...]`, phases with no failed host left
    out (PLAN.md WS-8)."""
    if run.state != 'awaiting_approval':
        return []
    out = []
    for phase in RETRYABLE_AT.get(run.awaiting_phase, ()):
        failed = [h.hostname for h in run.hosts
                  if h.state == 'failed' and h.last_phase == phase]
        if failed:
            out.append((phase, failed))
    return out


def retry(*, run, phase, user, password, public_key):
    """Run a phase again on the hosts that failed it (PLAN.md WS-8).

    Allowed while the run waits at a gate, for a phase that ran since the gate
    before it (`models.RETRYABLE_AT`). It is an approval like any other: it
    collects the retrier's credential and records who approved it. The job is
    marked `is_retry`, and the sibling puts the failed hosts' cursors back when
    it starts it -- Flask writes no per-host state (§7.3). When the retry ends,
    the run carries on from that phase as it did the first time, which lands
    it back at the same gate.
    """
    _check_gate(run)
    allowed = dict(retryable_phases(run))
    if phase not in allowed:
        if phase in RETRYABLE_AT.get(run.awaiting_phase, ()):
            raise RequestError(f'No host has failed {phase}; there is nothing to retry.')
        raise RequestError(
            f'{phase} cannot be retried while the run waits at the '
            f'{run.awaiting_phase} gate.'
        )
    return _queue_from_gate(run=run, phase=phase, user=user, password=password,
                            public_key=public_key, is_retry=True,
                            hosts=len(allowed[phase]))


def _check_gate(run):
    if run.state != 'awaiting_approval':
        raise RequestError(f'This run is {run.state}, not waiting at a gate.')
    expires = _aware(run.gate_expires_at)
    if expires is not None and _utcnow() >= expires:
        # The sibling moves the run to `expired` on its next loop. Refusing
        # here too means the TTL holds even while the sibling is down, without
        # Flask writing the expiry edge itself (§7.3 gives that to the sibling).
        raise RequestError('This gate has expired. Submit a new run.')


def _queue_from_gate(*, run, phase, user, password, public_key, is_retry, hosts):
    """Queue `phase` from the gate the run is waiting at, and take it off the gate.

    The attempt is one past the highest so far for this phase, so an abandoned
    attempt and a retry follow the same rule (PLAN.md WS-8).

    Leaving the gate is a conditional update, the claim pattern: two requests
    at one gate -- an approval and a retry, or two approvals under
    `gunicorn.conf.py`'s threads -- both pass the checks above, and only one
    may take the run off it. The loser gets a message, not a second job.
    """
    _require_device_username(user)
    _check_credential(user, password)
    if UpgradePhaseJob.query.filter(
            UpgradePhaseJob.run_id == run.id,
            UpgradePhaseJob.status.in_(('queued', 'running'))).first():
        # A run at a gate has nothing queued or running; one that does was
        # already acted on, whatever its state column says.
        raise RequestError('That gate has already been acted on.')
    previous = (db.session.query(db.func.max(UpgradePhaseJob.attempt))
                .filter_by(run_id=run.id, phase=phase).scalar())

    # The run's own rows carry the image size -- read from there rather than
    # from `artifacts`, which is what keeps a run self-contained and stops a
    # mid-run supersede re-targeting it (§5).
    job = UpgradePhaseJob(
        run_id=run.id, phase=phase, attempt=(previous or 0) + 1, status='queued',
        is_retry=is_retry, approved_by=user.id, approved_at=_utcnow(),
        created_at=_utcnow(),
        deadline_at=phase_deadline(
            phase, hosts=hosts,
            image_bytes=max((h.file_size or 0) for h in run.hosts) if run.hosts else 0,
        ),
    )
    left = (
        db.session.query(UpgradeRun)
        .filter(UpgradeRun.id == run.id, UpgradeRun.state == 'awaiting_approval',
                UpgradeRun.awaiting_phase == run.awaiting_phase)
        .update({'state': 'running', 'awaiting_phase': None, 'gate_expires_at': None},
                synchronize_session='fetch')
    )
    if left != 1:
        db.session.rollback()
        raise RequestError('That gate has already been acted on.')
    db.session.add(job)
    try:
        db.session.flush()
    except IntegrityError:
        # UNIQUE(run_id, phase, attempt): the same phase queued by a request
        # that left the gate first. Never two reloads either way.
        db.session.rollback()
        raise RequestError('That gate has already been acted on.') from None
    _seal_into(job, user=user, password=password, public_key=public_key)
    db.session.commit()
    return job


def decline_cleanup(*, run):
    """Declining is an edge, not an absence.

    Without it, "awaiting cleanup approval" and "finished, cleanup declined"
    are the same row (§7.3).
    """
    if run.state != 'awaiting_approval' or run.awaiting_phase != 'cleanup':
        raise RequestError('This run is not waiting at the cleanup gate.')
    run.state = 'completed'
    run.awaiting_phase = None
    run.gate_expires_at = None
    run.finished_at = _utcnow()
    db.session.commit()
    return run


def request_cancel(*, run, user):
    """Ask for a stop. The sibling polls this column between hosts.

    A cancel is a column and not a signal or a kill, because the job row is the
    only control channel (§9). What cancel *means* is per phase: cancelling a
    stage is safe, cancelling an activation mid-wave is not.
    """
    if run.state in ('completed', 'failed', 'cancelled', 'expired'):
        raise RequestError(f'This run is already {run.state}.')
    run.cancel_requested_at = _utcnow()
    run.cancel_requested_by = user.id
    if run.state == 'awaiting_approval':
        # Nothing is running, so no sibling will ever see the column.
        run.state = 'cancelled'
        run.awaiting_phase = None
        run.finished_at = _utcnow()
    # A queued job will now be finished `cancelled` without being claimed, so
    # its credential will never be opened: drop the ciphertext now rather
    # than leave it until the sibling gets there. Not a job-status edge (the
    # sibling still writes `cancelled`), and only `queued` rows can hold one.
    UpgradePhaseJob.query.filter_by(run_id=run.id, status='queued').update(
        {'sealed_credential': None}, synchronize_session='fetch'
    )
    db.session.commit()
    return run
