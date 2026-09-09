"""Checks for the manual escape hatch.

No device. What matters here is that the bypass is a bypass of the *web app*
and not of the safety properties -- most of these assert a refusal.
"""

import pytest

from nethub import upgrade_cli
from nethub.devices import connection


class TestPinResolution:
    def test_a_supplied_fingerprint_is_used(self):
        pin = upgrade_cli.resolve_pin(
            "192.0.2.10", "ssh-rsa SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq")
        assert pin.key_type == "ssh-rsa"
        assert pin.fingerprint_sha256 == "SHA256:C8F5CyUEoG/8/+TwtcUTvXpMpq"

    def test_a_bare_digest_defaults_to_the_common_key_type(self):
        assert upgrade_cli.resolve_pin("192.0.2.10", "SHA256:abc").key_type == "ssh-rsa"

    def test_no_pin_and_no_fingerprint_refuses_rather_than_trusting(self, monkeypatch):
        """The one thing this tool must never become is a way around §4.3."""
        monkeypatch.setattr(upgrade_cli, "_pin_from_database", lambda host: None)
        with pytest.raises(SystemExit) as excinfo:
            upgrade_cli.resolve_pin("192.0.2.10", None)
        assert "--scan" in str(excinfo.value)
        assert "compare" in str(excinfo.value).lower()

    def test_an_unreachable_database_is_not_fatal_by_itself(self, monkeypatch):
        """NetHub being down is the reason this tool exists."""
        def boom(host):
            raise RuntimeError("no database")
        monkeypatch.setattr(upgrade_cli, "_pin_from_database",
                            upgrade_cli._pin_from_database)
        monkeypatch.setattr("nethub.sibling._database_app", boom)
        assert upgrade_cli._pin_from_database("192.0.2.10") is None

    def test_a_confirmed_row_is_preferred_when_the_database_answers(self, monkeypatch):
        monkeypatch.setattr(upgrade_cli, "_pin_from_database",
                            lambda host: connection.HostKey("ssh-ed25519", "SHA256:db"))
        pin = upgrade_cli.resolve_pin("192.0.2.10", None)
        assert pin.fingerprint_sha256 == "SHA256:db"


class TestArgumentGuards:
    def run(self, argv):
        with pytest.raises(SystemExit) as excinfo:
            upgrade_cli.main(argv)
        return excinfo.value.code

    def test_an_unknown_phase_is_refused(self):
        assert self.run(["--host", "192.0.2.10", "--user", "me",
                         "--phases", "reboot"]) == 2

    def test_image_details_are_required_for_device_phases(self):
        assert self.run(["--host", "192.0.2.10", "--user", "me",
                         "--phases", "stage"]) == 2

    def test_host_is_required(self):
        assert self.run(["--user", "me"]) == 2

    def test_cleanup_alone_needs_no_image(self, monkeypatch):
        """It removes inactive packages; there is nothing to name."""
        monkeypatch.setattr(upgrade_cli, "resolve_pin",
                            lambda host, fp: connection.HostKey("ssh-rsa", "SHA256:x"))
        monkeypatch.setattr(upgrade_cli, "_confirm", lambda prompt, yes: False)
        monkeypatch.setattr(connection, "connect",
                            lambda *a, **kw: _FakeConn())
        monkeypatch.setenv("NETHUB_DEVICE_PASSWORD", "pw")
        # Declining the confirmation exits 1 rather than erroring on arguments.
        assert upgrade_cli.main(["--host", "192.0.2.10", "--user", "me",
                                 "--phases", "cleanup"]) == 1


class _FakeConn:
    def disconnect(self):
        pass


class _FakeFacts:
    version = "17.12.06"


class TestConfirmations:
    def test_every_device_changing_phase_is_confirmed(self):
        assert upgrade_cli.MUTATING == {"stage", "activate", "cleanup"}
        assert "precheck" not in upgrade_cli.MUTATING
        assert "verify" not in upgrade_cli.MUTATING

    def test_declining_stops_before_the_phase_runs(self, monkeypatch, tmp_path):
        image = tmp_path / "img.bin"
        image.write_bytes(b"x")
        staged = []
        monkeypatch.setattr(upgrade_cli, "resolve_pin",
                            lambda host, fp: connection.HostKey("ssh-rsa", "SHA256:x"))
        monkeypatch.setattr(connection, "connect", lambda *a, **kw: _FakeConn())
        monkeypatch.setattr(upgrade_cli.transfer, "stage_image",
                            lambda *a, **kw: staged.append(1))
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        monkeypatch.setenv("NETHUB_DEVICE_PASSWORD", "pw")
        code = upgrade_cli.main([
            "--host", "192.0.2.10", "--user", "me", "--phases", "stage",
            "--image", str(image), "--sha512", "a" * 128, "--version", "17.12.06"])
        assert code == 1 and staged == []

    def test_yes_skips_the_prompt(self, monkeypatch, tmp_path):
        image = tmp_path / "img.bin"
        image.write_bytes(b"x")
        staged = []

        class Outcome:
            status, scp_restore_confirmed = "image_copied", True

        monkeypatch.setattr(upgrade_cli, "resolve_pin",
                            lambda host, fp: connection.HostKey("ssh-rsa", "SHA256:x"))
        monkeypatch.setattr(connection, "connect", lambda *a, **kw: _FakeConn())
        monkeypatch.setattr(upgrade_cli.transfer, "stage_image",
                            lambda *a, **kw: staged.append(1) or Outcome())
        monkeypatch.setattr("builtins.input",
                            lambda prompt: pytest.fail("should not prompt"))
        monkeypatch.setenv("NETHUB_DEVICE_PASSWORD", "pw")
        code = upgrade_cli.main([
            "--host", "192.0.2.10", "--user", "me", "--phases", "stage", "--yes",
            "--image", str(image), "--sha512", "a" * 128, "--version", "17.12.06"])
        assert code == 0 and staged == [1]


class TestItWritesNothing:
    def test_the_cli_imports_no_job_or_run_models(self):
        """§7.3 gives every job-row edge to Flask or the sibling. A third
        writer would put rows in the database no approval accounts for."""
        source = __import__("pathlib").Path("nethub/upgrade_cli.py").read_text()
        for forbidden in ("UpgradeRun", "UpgradePhaseJob", "UpgradeHostPhaseResult",
                          "db.session"):
            assert forbidden not in source, f"{forbidden} must not be written here"

    def test_it_warns_on_every_invocation(self, capsys, monkeypatch):
        monkeypatch.setattr(upgrade_cli, "resolve_pin",
                            lambda host, fp: connection.HostKey("ssh-rsa", "SHA256:x"))
        monkeypatch.setattr(connection, "connect", lambda *a, **kw: _FakeConn())
        monkeypatch.setattr(upgrade_cli.install, "verify_upgrade",
                            lambda conn, target_version: _FakeFacts())
        monkeypatch.setenv("NETHUB_DEVICE_PASSWORD", "pw")
        upgrade_cli.main(["--host", "192.0.2.10", "--user", "me", "--phases", "verify",
                          "--image", "/tmp/x.bin", "--sha512", "a" * 128,
                          "--version", "17.12.06"])
        out = capsys.readouterr().out
        assert "nothing below is written to the database" in out
