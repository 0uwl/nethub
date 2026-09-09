Real output from a Catalyst C9200CX-12P-2X2G running IOS-XE 17.12.06 in
INSTALL mode, captured 2026-09-09 with scripts/check_device_facts.py.

Keep these verbatim. They are the evidence that ntc-templates parses the
IOS-XE releases this fleet actually runs; editing them to make a test pass
throws that away. Add a directory per release rather than amending one.

`ssh_host_key.pub` is the same device's SSH host public key (public by
definition -- every client that connects is handed it). Its fingerprint,
per `ssh-keygen -lf`, is:

    SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq/7+0sBXfa2VoG+/xg

test_connection.py checks nethub.devices.connection.HostKey against that
string, so the fingerprint we pin is the one OpenSSH would show an admin
comparing it out-of-band.
