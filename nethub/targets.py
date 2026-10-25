"""What a run may name: `hostname, address` lines, addresses inside the
target CIDRs (docs/host-keys.md [target-is-an-ip-in-a-cidr]), and an
activate approval's reload count.

Pure, so the web app and `nethub.local_run` apply the same checks;
`upgrades.py` re-exports every name here.
"""

from __future__ import annotations

import ipaddress


class RequestError(Exception):
    """The submitted request cannot be compiled into a run."""



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
    would end up keyed on a string whose meaning can change afterwards.
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


def reload_count(raw, cap):
    """The approver's "reload N devices at a time after the first", or refuse
    it. Blank means the default, 1. `cap` is the deployment's
    `PHASE_CONCURRENCY`; the sibling caps it again, since the setting can be
    lowered before the job runs."""
    text = '' if raw is None else str(raw).strip()
    if not text:
        return 1
    try:
        count = int(text)
    except ValueError:
        count = 0
    if count < 1 or count > cap:
        raise RequestError(f'Reload count must be a whole number from 1 to {cap}.')
    return count
