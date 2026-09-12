"""Checks for nethub.devices.transfer -- the SCP bracket and the digest gate.

No device. The verify output format is the one a real 17.12.06 device emits
(`verify /sha512 (flash:packages.conf) = <128 hex>`), and DIGEST is that
device's actual digest for packages.conf.
"""

import pytest

from nethub.devices import transfer as T

DIGEST = (
    "701107e35b5e818bb5af64fcac5a65036e65dc4ae4bd09419b653ad0b845ddfd"
    "3ef7bd75bcbab0bd953e962c5390d9dc24eea20828f02ce0048c76bbda4cd56f"
)
OTHER_DIGEST = "a" * 128
IMAGE = "cat9k_lite_iosxe.17.12.06.SPA.bin"


def dir_output(files, free=3723296768, total=4967505920):
    lines = ["Directory of flash:/", ""]
    for i, (name, size) in enumerate(files.items(), start=100):
        lines.append(f"{i}    -rw-  {size:>14}   Sep 7 2026 12:53:44 +00:00  {name}")
    lines += ["", f"{total} bytes total ({free} bytes free)"]
    return "\n".join(lines) + "\n"


class FakeDevice:
    """Enough of a Netmiko connection for the transport logic."""

    def __init__(
        self, *, files=None, scp_enabled=False, digest=DIGEST, restorable=True, free=10**10
    ):
        # A real flash always holds something; the ntc-template for `dir`
        # emits no rows at all for an empty listing.
        self.files = {"packages.conf": 4905, **(files or {})}
        self.scp_enabled = scp_enabled
        self.digest = digest
        self.restorable = restorable
        self.free = free
        self.commands = []
        self.config_sets = []
        self.written = []

    def send_command(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("dir "):
            return dir_output(self.files, free=self.free)
        if command == T._SCP_SHOW:
            return "ip scp server enable\n" if self.scp_enabled else ""
        if command.startswith("verify /sha512"):
            target = command.split()[-1]
            if self.digest is None:
                return f"%Error opening {target} (No such file or directory)\n"
            return f".Done!\nverify /sha512 ({target}) = {self.digest}\n\n"
        raise AssertionError(f"unexpected command {command!r}")

    def send_config_set(self, lines):
        self.config_sets.append(list(lines))
        for line in lines:
            if line == T._SCP_ENABLE:
                self.scp_enabled = True
            elif line == f"no {T._SCP_ENABLE}" and self.restorable:
                self.scp_enabled = False

    # -- pull path --
    def write_channel(self, data):
        self.written.append(data)

    def read_until_pattern(self, pattern, **kwargs):
        return ""


@pytest.fixture
def image_on_disk(tmp_path):
    source = tmp_path / IMAGE
    source.write_bytes(b"x" * 4096)
    return tmp_path, 4096


@pytest.fixture
def no_scp_put(monkeypatch):
    """Replace the real SCP put; the bracket around it is what is under test."""
    calls = []
    monkeypatch.setattr(T, "_scp_put", lambda conn, **kw: calls.append(kw))
    return calls


class TestVerify:
    def test_parses_the_real_device_format(self):
        device = FakeDevice()
        assert T.verify_sha512(device, file_system="flash:", image=IMAGE, expected=DIGEST) == DIGEST

    def test_mismatch_reports_what_the_device_computed(self):
        device = FakeDevice(digest=OTHER_DIGEST)
        with pytest.raises(T.VerificationError, match=OTHER_DIGEST):
            T.verify_sha512(device, file_system="flash:", image=IMAGE, expected=DIGEST)

    def test_absent_digest_is_a_failure_not_a_pass(self):
        device = FakeDevice(digest=None)
        with pytest.raises(T.VerificationError, match="no SHA-512 digest"):
            T.verify_sha512(device, file_system="flash:", image=IMAGE, expected=DIGEST)

    def test_the_expected_digest_is_never_sent_to_the_device(self):
        """Handing the device the answer lets its echo satisfy the match."""
        device = FakeDevice()
        T.verify_sha512(device, file_system="flash:", image=IMAGE, expected=DIGEST)
        assert device.commands == [f"verify /sha512 flash:{IMAGE}"]
        assert DIGEST not in device.commands[0]

    def test_the_word_verified_alone_does_not_pass(self):
        device = FakeDevice()
        device.send_command = lambda command, **kw: "Verified flash:image\n"
        with pytest.raises(T.VerificationError):
            T.verify_sha512(device, file_system="flash:", image=IMAGE, expected=DIGEST)


class TestScpServerReading:
    def test_negated_line_reads_as_disabled(self):
        """`no ip scp server enable` contains the enabled form as a substring."""
        assert T._scp_server_enabled("no ip scp server enable\n") is False
        assert T._scp_server_enabled("ip scp server enable\n") is True
        assert T._scp_server_enabled("") is False


class TestPushBracket:
    def test_enables_transfers_and_restores_to_disabled(self, image_on_disk, no_scp_put):
        search_dir, size = image_on_disk
        device = FakeDevice(scp_enabled=False)

        out = T.stage_image(
            device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
            transport="push_scp", file_size=size,
        )

        assert out.status == "image_copied"
        assert out.scp_restore_confirmed is True
        assert device.config_sets == [[T._SCP_ENABLE], [f"no {T._SCP_ENABLE}"]]
        assert device.scp_enabled is False
        assert len(no_scp_put) == 1

    def test_an_already_enabled_server_is_left_enabled_and_not_re_enabled(
        self, image_on_disk, no_scp_put
    ):
        search_dir, size = image_on_disk
        device = FakeDevice(scp_enabled=True)

        out = T.stage_image(
            device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
            transport="push_scp", file_size=size,
        )

        assert out.scp_restore_confirmed is True
        assert device.config_sets == [[T._SCP_ENABLE]]  # restore only
        assert device.scp_enabled is True

    def test_unconfirmed_restore_fails_the_host(self, image_on_disk, no_scp_put):
        search_dir, size = image_on_disk
        device = FakeDevice(scp_enabled=False, restorable=False)

        with pytest.raises(T.ScpRestoreError) as excinfo:
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )

        assert excinfo.value.status == "scp_not_restored"
        assert excinfo.value.scp_restore_confirmed is False
        assert "by hand" in str(excinfo.value)

    def test_unconfirmed_restore_wins_over_a_push_failure(self, image_on_disk, monkeypatch):
        """A device left changed is the more urgent of the two facts."""
        search_dir, size = image_on_disk
        device = FakeDevice(scp_enabled=False, restorable=False)

        def boom(conn, **kw):
            raise OSError("link went away")

        monkeypatch.setattr(T, "_scp_put", boom)
        with pytest.raises(T.ScpRestoreError) as excinfo:
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )
        # The push failure is kept rather than lost.
        assert isinstance(excinfo.value.__context__, T.TransferError)
        assert "link went away" in str(excinfo.value.__context__)

    def test_push_failure_with_a_confirmed_restore_reports_the_push(
        self, image_on_disk, monkeypatch
    ):
        search_dir, size = image_on_disk
        device = FakeDevice(scp_enabled=False)

        def boom(conn, **kw):
            raise OSError("link went away")

        monkeypatch.setattr(T, "_scp_put", boom)
        with pytest.raises(T.TransferError) as excinfo:
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )
        assert excinfo.value.status == "not_copied"
        assert not isinstance(excinfo.value, T.ScpRestoreError)
        assert device.scp_enabled is False, "still restored on the failure path"


class TestSkipIfStaged:
    def test_matching_size_and_digest_skips_the_transfer_entirely(
        self, image_on_disk, no_scp_put
    ):
        search_dir, size = image_on_disk
        device = FakeDevice(files={IMAGE: size})

        out = T.stage_image(
            device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
            transport="push_scp", file_size=size,
        )

        assert out.status == "already_staged"
        assert no_scp_put == []
        assert device.config_sets == [], "no device configuration was touched"

    def test_right_name_wrong_bytes_is_not_staged(self, image_on_disk, no_scp_put):
        """A file of the right name is not the file staging checked."""
        search_dir, size = image_on_disk
        device = FakeDevice(files={IMAGE: size}, digest=OTHER_DIGEST)

        with pytest.raises(T.VerificationError):
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )
        assert len(no_scp_put) == 1, "it re-pushed rather than trusting the name"


class TestSource:
    def test_declared_size_mismatch_stops_before_any_transfer(self, image_on_disk):
        search_dir, size = image_on_disk
        with pytest.raises(T.TransferError, match="declared as"):
            T.resolve_source(str(search_dir), IMAGE, declared_size=size + 1)

    def test_measurement_is_used_when_nothing_is_declared(self, image_on_disk):
        search_dir, size = image_on_disk
        assert T.resolve_source(str(search_dir), IMAGE)[1] == size

    def test_missing_source_is_an_error(self, tmp_path):
        with pytest.raises(T.TransferError, match="cannot read"):
            T.resolve_source(str(tmp_path), IMAGE)


class TestPull:
    def test_password_never_reaches_the_command_line(self, image_on_disk):
        search_dir, size = image_on_disk
        device = FakeDevice()
        target = T.PullTarget("dist.example.net", "nethub", "s3cret")

        out = T.stage_image(
            device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
            transport="pull_sftp", file_size=size, pull_target=target,
        )

        command = device.written[0]
        assert command.startswith("copy sftp://nethub@dist.example.net")
        assert "s3cret" not in command
        assert ":" not in command.split("@")[0].removeprefix("copy sftp://")
        assert device.written[1] == IMAGE + "\n"
        assert device.written[2] == "s3cret\n"
        assert out.status == "image_copied"

    def test_pull_reconfigures_nothing(self, image_on_disk):
        search_dir, size = image_on_disk
        device = FakeDevice()
        out = T.stage_image(
            device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
            transport="pull_sftp", file_size=size,
            pull_target=T.PullTarget("dist.example.net", "nethub", "s3cret"),
        )
        assert device.config_sets == []
        assert out.scp_restore_confirmed is None, "null means no bracket ran, not 'failed'"

    def test_pull_without_a_target_is_refused(self, image_on_disk):
        search_dir, size = image_on_disk
        with pytest.raises(T.TransferError, match="needs a distribution host"):
            T.stage_image(
                FakeDevice(), image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="pull_sftp", file_size=size,
            )


class TestGuards:
    def test_unknown_transport_is_refused_before_anything_happens(self, image_on_disk):
        search_dir, size = image_on_disk
        device = FakeDevice()
        with pytest.raises(T.TransferError, match="push_scp"):
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="scp", file_size=size,
            )
        assert device.commands == []

    @pytest.mark.parametrize("name", ["a b.bin", "a;reload.bin", "../a.bin", "a|b.bin", ""])
    def test_image_names_that_reach_the_cli_are_checked(self, name, image_on_disk):
        search_dir, _ = image_on_disk
        with pytest.raises(T.TransferError, match="invalid image name"):
            T.stage_image(
                FakeDevice(), image=name, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp",
            )

    def test_a_non_sha512_digest_is_refused(self, image_on_disk):
        search_dir, size = image_on_disk
        with pytest.raises(T.TransferError, match="not a SHA-512 digest"):
            T.stage_image(
                FakeDevice(), image=IMAGE, sha512="deadbeef", search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )

    def test_insufficient_free_space_stops_before_the_transfer(self, image_on_disk, no_scp_put):
        search_dir, size = image_on_disk
        device = FakeDevice(free=size - 1)
        with pytest.raises(T.TransferError, match="bytes free"):
            T.stage_image(
                device, image=IMAGE, sha512=DIGEST, search_dir=str(search_dir),
                transport="push_scp", file_size=size,
            )
        assert no_scp_put == []
        assert device.config_sets == []
