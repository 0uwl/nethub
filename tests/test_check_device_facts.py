"""`scripts/check_device_facts.py`: where its host-key pin comes from.

No device. What matters is that a script that works with NetHub down is not
a way around the pin (docs/device-layer.md [facts-script-never-tofu]).
"""

import importlib.util
from pathlib import Path

import pytest

from nethub.devices import connection

_spec = importlib.util.spec_from_file_location(
    "check_device_facts", Path(__file__).parent.parent / "scripts" / "check_device_facts.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


class TestPinResolution:
    def test_a_supplied_fingerprint_is_used(self):
        pin = script.resolve_pin(
            "192.0.2.10", "ssh-rsa SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq")
        assert pin.key_type == "ssh-rsa"
        assert pin.fingerprint_sha256 == "SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq"

    def test_a_bare_digest_defaults_to_the_common_key_type(self):
        assert script.resolve_pin("192.0.2.10", "SHA256:abc").key_type == "ssh-rsa"

    def test_no_pin_and_no_fingerprint_refuses_rather_than_trusting(self, monkeypatch):
        """The one thing this script must never become is a way around the pin."""
        monkeypatch.setattr(script, "_pin_from_database", lambda host: None)
        with pytest.raises(SystemExit) as excinfo:
            script.resolve_pin("192.0.2.10", None)
        assert "--scan" in str(excinfo.value)
        assert "compare" in str(excinfo.value).lower()

    def test_an_unreachable_database_is_not_fatal_by_itself(self, monkeypatch):
        """The script must work with NetHub down."""
        def boom(host):
            raise RuntimeError("no database")
        monkeypatch.setattr("nethub.sibling._database_app", boom)
        assert script._pin_from_database("192.0.2.10") is None

    def test_a_confirmed_row_is_preferred_when_the_database_answers(self, monkeypatch):
        monkeypatch.setattr(script, "_pin_from_database",
                            lambda host: connection.HostKey("ssh-ed25519", "SHA256:db"))
        pin = script.resolve_pin("192.0.2.10", None)
        assert pin.fingerprint_sha256 == "SHA256:db"
