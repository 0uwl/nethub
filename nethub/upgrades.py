"""Compiling an upgrade request into a run, and moving it through its gates.

The routes are thin over this, the way `registry_routes.py` is thin over
`registry.py`. Everything that decides whether a run may exist lives here.

A submitter sends a *request document* -- hosts, one bundle key each, and
nothing else -- which NetHub validates and compiles into rows (design doc
§8.1). There is no inventory to upload and no template to render: the only
connection var a request may carry is the target address, and it is validated
rather than trusted. Everything that says *who someone is* is read
server-side.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timezone

from .extensions import db
from .models import (
    DeviceHostKey,
    UpgradePhaseJob,
    UpgradeRun,
    UpgradeRunHost,
)

#: Which phases a human approves. `precheck` runs on submit with no gate, and
#: `verify` follows `activate` automatically -- both are read-only (§8.1).
APPROVABLE = ('stage', 'activate', 'cleanup')


class RequestError(Exception):
    """The submitted request cannot be compiled into a run."""


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


def entry_for(entries: dict, bundle: str) -> dict:
    entry = entries.get(bundle)
    if not isinstance(entry, dict):
        raise RequestError(f'No registry entry named "{bundle}".')
    missing = [f for f in ('file_name', 'sha512', 'file_size', 'version') if f not in entry]
    if missing:
        raise RequestError(f'Registry entry "{bundle}" is missing: {", ".join(missing)}.')
    return entry


def build_document(bundle: str, hosts, registry_name: str) -> str:
    """The request as NetHub understood it, stored beside its digest.

    §3.4 hashes what it ingests, and a document deciding which images land on
    which devices is not the exception -- but a digest whose preimage is stored
    nowhere is unverifiable, so both go in the row.
    """
    return json.dumps(
        {
            'registry': registry_name,
            'bundle': bundle,
            'hosts': [{'hostname': h, 'ansible_host': a} for h, a in hosts],
        },
        sort_keys=True,
        separators=(',', ':'),
    )


def submit(*, user, registry, entries, bundle, hosts_raw, transport,
           cidrs, shared_account_mode=False, flash_dir='flash:'):
    """Compile a request into a run, its host rows, and a queued pre-check.

    Flask writes exactly one job edge in the whole system and this is it: the
    row that arrives `queued` (§7.3). Everything after dispatch is the
    sibling's.
    """
    if not user.device_username:
        raise RequestError(
            'Your device username is not set. NetHub maps it server-side and '
            'will not take it from a request -- set it on your profile first.'
        )
    hosts = parse_hosts(hosts_raw)
    entry = entry_for(entries, bundle)
    targets = []
    for hostname, address in hosts:
        checked = check_target(address, cidrs)
        confirmed_key(checked)
        targets.append((hostname, checked))

    run = UpgradeRun(
        submitted_by=user.id,
        device_username_used=user.device_username,
        shared_account_mode=bool(shared_account_mode),
        image_transport_used=transport,
        request_document=build_document(bundle, targets, registry.name),
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
            bundle_key=bundle,
            filename=entry['file_name'],
            sha512=str(entry['sha512']).lower(),
            version=str(entry['version']),
            file_size=int(entry['file_size']),
            flash_dir=flash_dir,
            state='pending',
        ))

    job = UpgradePhaseJob(
        run_id=run.id, phase='precheck', attempt=1, status='queued',
        created_at=_utcnow(),
    )
    db.session.add(job)
    db.session.commit()
    return run, job


def approve(*, run, phase, user):
    """Write the phase job a gate is waiting for.

    Two admins both clicking "approve: reload" is the case this has to refuse:
    §8.1's serialization is scoped to *execution*, so it would otherwise queue
    two reloads that then run one after the other. `UNIQUE(run_id, phase,
    attempt)` is where that collision is caught, and the check below turns it
    into a message rather than an IntegrityError.
    """
    if run.state != 'awaiting_approval':
        raise RequestError(f'This run is {run.state}, not waiting at a gate.')
    if phase not in APPROVABLE:
        raise RequestError(f'"{phase}" is not a phase anyone approves.')
    if run.awaiting_phase != phase:
        raise RequestError(
            f'This run is waiting at the {run.awaiting_phase} gate, not {phase}.'
        )
    attempt = 1 + (
        UpgradePhaseJob.query.filter_by(run_id=run.id, phase=phase)
        .filter(UpgradePhaseJob.status.in_(('abandoned',)))
        .count()
    )
    if UpgradePhaseJob.query.filter_by(
        run_id=run.id, phase=phase, attempt=attempt
    ).first():
        raise RequestError('That phase has already been approved.')

    job = UpgradePhaseJob(
        run_id=run.id, phase=phase, attempt=attempt, status='queued',
        approved_by=user.id, approved_at=_utcnow(), created_at=_utcnow(),
    )
    db.session.add(job)
    run.state = 'running'
    run.awaiting_phase = None
    run.gate_expires_at = None
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
    db.session.commit()
    return run
