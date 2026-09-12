"""Checks for the artifact store that replaced the software_registry YAML.

The refusals are the point. Everything here is something the YAML store also
had to get right, plus the two constraints only a table can express.
"""

import hashlib
import io
import os

import pytest
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage

from nethub import artifacts
from nethub.extensions import db
from nethub.models import Artifact

CONTENT = b"pretend-ios-xe-image" * 100
DIGEST = hashlib.sha512(CONTENT).hexdigest()
IMAGE = "cat9k_lite_iosxe.17.12.06.SPA.bin"


def upload(content=CONTENT, filename=IMAGE):
    return FileStorage(stream=io.BytesIO(content), filename=filename)


def ingest(store, **kw):
    kw.setdefault("file_storage", upload())
    kw.setdefault("bundle_key", "iosxe-17-12-06")
    kw.setdefault("version", "17.12.06")
    kw.setdefault("sha512", DIGEST)
    kw.setdefault("uploaded_by", None)
    return artifacts.ingest(store=store, **kw)


@pytest.fixture
def store(app):
    with app.app_context():
        yield artifacts.store_dir(app.config)


class TestIngest:
    def test_a_good_upload_is_recorded_and_stored(self, app, store):
        with app.app_context():
            a = ingest(store)
            assert (a.bundle_key, a.version, a.state) == ("iosxe-17-12-06", "17.12.06", "published")
            assert a.file_size == len(CONTENT)
            assert os.path.isfile(a.storage_path)
            with open(a.storage_path, "rb") as handle:
                assert handle.read() == CONTENT

    def test_the_digest_recorded_is_the_one_computed(self, app, store):
        """The uploader's claim is what is being verified, never what is kept."""
        with app.app_context():
            assert ingest(store).sha512 == hashlib.sha512(CONTENT).hexdigest()

    def test_a_checksum_mismatch_leaves_nothing_behind(self, app, store):
        with app.app_context():
            with pytest.raises(artifacts.ArtifactError, match="Checksum mismatch"):
                ingest(store, sha512="b" * 128)
            assert Artifact.query.count() == 0
            assert os.listdir(store) == [], "no partial file and no temp file"

    def test_an_interrupted_upload_leaves_no_temp_file(self, app, store):
        class Boom(io.BytesIO):
            def read(self, n=-1):
                raise OSError("connection reset")

        with app.app_context():
            with pytest.raises(OSError):
                ingest(store, file_storage=FileStorage(stream=Boom(), filename=IMAGE))
            assert os.listdir(store) == []

    def test_nothing_lands_at_the_final_path_until_it_verifies(self, app, store):
        with app.app_context():
            with pytest.raises(artifacts.ArtifactError):
                ingest(store, sha512="c" * 128)
            assert not os.path.exists(os.path.join(store, IMAGE))

    @pytest.mark.parametrize("bad", ["", "deadbeef", "z" * 128, "A" * 127])
    def test_a_non_sha512_is_refused(self, app, store, bad):
        with app.app_context(), pytest.raises(artifacts.ArtifactError, match="128-character"):
            ingest(store, sha512=bad)

    def test_version_is_required(self, app, store):
        with app.app_context(), pytest.raises(artifacts.ArtifactError, match="Version"):
            ingest(store, version="")

    @pytest.mark.parametrize("bad", ["", "a key", "../etc", "x" * 81])
    def test_a_bad_bundle_key_is_refused(self, app, store, bad):
        with app.app_context(), pytest.raises(artifacts.ArtifactError, match="Bundle key"):
            ingest(store, bundle_key=bad)

    def test_an_empty_file_is_refused(self, app, store):
        with app.app_context(), pytest.raises(artifacts.ArtifactError, match="empty"):
            ingest(store, file_storage=upload(content=b""),
                   sha512=hashlib.sha512(b"").hexdigest())

    def test_a_traversing_filename_is_reduced_to_a_bare_name(self, app, store):
        with app.app_context():
            a = ingest(store, file_storage=upload(filename="../../etc/passwd"))
            assert os.path.dirname(a.storage_path) == store
            assert "/" not in a.filename


class TestConstraints:
    def test_one_published_artifact_per_bundle_key(self, app, store):
        with app.app_context():
            ingest(store)
            with pytest.raises(artifacts.ArtifactError, match="already published"):
                ingest(store, file_storage=upload(filename="other.bin"))

    def test_two_artifacts_cannot_share_a_live_filename(self, app, store):
        """Both transports address the source by filename, so a shared name
        means one artifact's bytes silently overwrite another's."""
        with app.app_context():
            ingest(store)
            with pytest.raises(artifacts.ArtifactError, match="filename"):
                ingest(store, bundle_key="iosxe-17-12-08")

    def test_the_database_refuses_a_duplicate_filename_too(self, app, store):
        """Not only the service layer -- §5 puts this in the schema."""
        with app.app_context():
            ingest(store)
            db.session.add(Artifact(
                kind="image", platform="iosxe", bundle_key="other", filename=IMAGE,
                sha512="a" * 128, file_size=1, storage_path="/x", version="1",
                state="published"))
            with pytest.raises(IntegrityError):
                db.session.commit()

    def test_the_database_refuses_a_duplicate_bundle_key_too(self, app, store):
        with app.app_context():
            ingest(store)
            db.session.add(Artifact(
                kind="image", platform="iosxe", bundle_key="iosxe-17-12-06",
                filename="other.bin", sha512="a" * 128, file_size=1,
                storage_path="/x", version="1", state="published"))
            with pytest.raises(IntegrityError):
                db.session.commit()


class TestResolveAndCheck:
    def test_a_request_resolves_a_key_to_a_row(self, app, store):
        with app.app_context():
            ingest(store)
            assert artifacts.get_published("iosxe-17-12-06").filename == IMAGE

    def test_an_unknown_key_is_refused(self, app, store):
        with app.app_context(), pytest.raises(artifacts.ArtifactError, match="No published"):
            artifacts.get_published("nope")

    def test_a_pruned_artifact_is_refused(self, app, store):
        with app.app_context():
            ingest(store).bytes_state = "pruned"
            db.session.commit()
            with pytest.raises(artifacts.ArtifactError, match="pruned"):
                artifacts.get_published("iosxe-17-12-06")

    def test_check_store_is_quiet_when_the_bytes_match(self, app, store):
        with app.app_context():
            ingest(store)
            assert artifacts.check_store(store) == []

    def test_check_store_reports_altered_bytes_and_never_guesses(self, app, store):
        with app.app_context():
            a = ingest(store)
            recorded = a.sha512
            with open(a.storage_path, "wb") as handle:
                handle.write(b"something else entirely")
            issues = artifacts.check_store(store)
            assert len(issues) == 1 and "no longer matches" in issues[0]
            assert Artifact.query.one().sha512 == recorded, "a wrong digest is never re-derived"

    def test_check_store_silently_corrects_a_stale_size(self, app, store):
        """Cheap, non-security, and always recoverable from the file itself."""
        with app.app_context():
            a = ingest(store)
            a.file_size = 1
            db.session.commit()
            assert artifacts.check_store(store) == []
            assert Artifact.query.one().file_size == len(CONTENT)

    def test_check_store_reports_a_missing_file(self, app, store):
        with app.app_context():
            os.remove(ingest(store).storage_path)
            assert "missing" in artifacts.check_store(store)[0]


class TestDelete:
    def test_delete_removes_the_row_and_the_bytes(self, app, store):
        with app.app_context():
            a = ingest(store)
            path = a.storage_path
            artifacts.delete(a)
            assert Artifact.query.count() == 0
            assert not os.path.exists(path)


# --- The ingest race (WS-5.3) ------------------------------------------------
#
# The three checks at the top of `ingest` all run before the upload streams,
# so under concurrency they prove nothing by the time the bytes are moved into
# place. These tests drive that window directly rather than trying to time two
# real uploads against each other.

OTHER = b"a-different-image-entirely" * 100
OTHER_DIGEST = hashlib.sha512(OTHER).hexdigest()


def _win_the_race_during(store, content=OTHER):
    """Return a stream that plants `content` at the final path mid-upload.

    Simulates the other uploader finishing while this one is still streaming:
    by the time our `os.link` runs, the name is taken.
    """
    final = os.path.join(store, IMAGE)

    class _Racing(io.BytesIO):
        def read(self, *a, **kw):
            chunk = super().read(*a, **kw)
            if not chunk and not os.path.lexists(final):
                with open(final, "wb") as fh:
                    fh.write(content)
            return chunk

    return FileStorage(stream=_Racing(CONTENT), filename=IMAGE)


def test_a_lost_race_does_not_overwrite_the_winners_bytes(store, app):
    with app.app_context(), pytest.raises(artifacts.ArtifactError, match="already in the store"):
        ingest(store, file_storage=_win_the_race_during(store))

    # The winner's bytes are untouched -- this is the whole point. Before the
    # fix, os.replace clobbered them and left the winner's row pointing at the
    # loser's content, with every downstream hash check then failing.
    with open(os.path.join(store, IMAGE), "rb") as fh:
        assert fh.read() == OTHER


def test_a_lost_race_leaves_no_temp_file_behind(store, app):
    with app.app_context(), pytest.raises(artifacts.ArtifactError):
        ingest(store, file_storage=_win_the_race_during(store))
    leftovers = [n for n in os.listdir(store) if n.startswith(".incoming-")]
    assert leftovers == []


def test_a_lost_race_writes_no_row(store, app):
    with app.app_context():
        with pytest.raises(artifacts.ArtifactError):
            ingest(store, file_storage=_win_the_race_during(store))
        assert Artifact.query.filter_by(filename=IMAGE).count() == 0


def test_a_row_that_loses_at_commit_removes_its_own_bytes(store, app, monkeypatch):
    """The schema is the backstop, and it fires after the bytes are in place.

    A file no row accounts for would block that filename for every later
    upload, so losing this race has to clean up after itself.
    """
    with app.app_context():
        real_commit = db.session.commit
        calls = []

        def failing_commit():
            calls.append(1)
            if len(calls) == 1:
                raise IntegrityError("forced", None, Exception("forced"))
            return real_commit()

        monkeypatch.setattr(db.session, "commit", failing_commit)
        with pytest.raises(artifacts.ArtifactError, match="claimed the same"):
            ingest(store)

    assert not os.path.lexists(os.path.join(store, IMAGE))
    assert [n for n in os.listdir(store) if n.startswith(".incoming-")] == []


def test_the_happy_path_still_removes_its_temp_file(store, app):
    with app.app_context():
        artifact = ingest(store)
        assert artifact.sha512 == DIGEST
    assert os.path.isfile(os.path.join(store, IMAGE))
    assert [n for n in os.listdir(store) if n.startswith(".incoming-")] == []
    # One link only: the temp was unlinked, not left as a second name.
    assert os.stat(os.path.join(store, IMAGE)).st_nlink == 1
