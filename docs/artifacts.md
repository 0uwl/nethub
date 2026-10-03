# Artifacts
Code: `nethub/artifacts.py`, `nethub/artifact_routes.py`, `nethub/models.py` (`Artifact`, `ArtifactAudit`), `nethub/upgrades.py` (`resolve_bundle`) · Tests: `tests/test_artifacts.py`, `tests/test_roles.py` (`TestArtifactRule`), `tests/test_errors.py`

---

## AI READING INSTRUCTION

Read `[SPEC]` and `[BUG]` blocks for authoritative facts.
Read `[NOTE]` only if additional context is needed.
`[?]` blocks are unverified; treat them with lower confidence.
§1–§3 are rules: each H3 heading is a rule's slug. §4 explains how it
works, §5 decisions, §6 pitfalls, §7 known gaps.

---

## 1. Ingest

### 1.1 [hash-while-writing]
**[SPEC]**
`ingest()` streams the upload in 1 MiB chunks to a temp file
(`.incoming-*`) in the store, updating the SHA-512 as it goes. Nothing
re-reads the file. Uploading is publishing: it all happens synchronously
in the request, with no job and nothing dispatched.
Pinned by: `tests/test_artifacts.py::TestIngest::test_a_good_upload_is_recorded_and_stored`

**[NOTE]**
A 1.2 GB upload is the longest thing the web process does. A second
hashing pass would double that time.

### 1.2 [submitted-checksum-is-the-claim]
**[SPEC]**
The submitted SHA-512 is compared with the computed one, and a mismatch
leaves nothing behind. The row records the *computed* digest, never the
submitted one.
Pinned by: `tests/test_artifacts.py::TestIngest::test_a_checksum_mismatch_leaves_nothing_behind`, `tests/test_artifacts.py::TestIngest::test_the_digest_recorded_is_the_one_computed`

### 1.3 [link-never-replace]
**[SPEC]**
- The verified temp file reaches its final path through `os.link`, never
  `os.replace`.
- If the target already exists, the upload is refused, and the winner's
  bytes are never overwritten.
- The temp file is always removed.

Pinned by: `tests/test_artifacts.py::test_a_lost_race_does_not_overwrite_the_winners_bytes`, `tests/test_artifacts.py::test_a_lost_race_leaves_no_temp_file_behind`, `tests/test_artifacts.py::TestIngest::test_nothing_lands_at_the_final_path_until_it_verifies`

**[NOTE]**
Every uniqueness check at the top of `ingest()` runs before the upload
streams, so by the time the bytes move, minutes later, those checks prove
nothing. With `os.replace`, the loser of a race overwrote the winner's
committed bytes, so the winner's row recorded one image's digest against
another image's content.

### 1.4 [losing-commit-removes-its-bytes]
**[SPEC]**
If the row's commit hits an `IntegrityError`, which the schema backstop
raises after the bytes are in place, `ingest()` removes the file it just
linked. A file that no row accounts for would block that filename for
every later upload.
Pinned by: `tests/test_artifacts.py::test_a_row_that_loses_at_commit_removes_its_own_bytes`, `tests/test_artifacts.py::test_a_lost_race_writes_no_row`

### 1.5 [ingest-input-checks]
**[SPEC]**
Before any bytes stream, `ingest()` checks:
- bundle key `^[\w.+-]{1,80}$` (`\w` is Unicode-aware);
- version non-empty;
- SHA-512 is 128 hex characters (lower-cased);
- a file was sent, and its name survives `werkzeug.secure_filename`;
- no live artifact already uses the filename;
- no file of that name (`os.path.lexists`, so a broken symlink counts)
  is in the store;
- unless the upload will land `staged`, nothing is already published
  under the key.

After streaming, `ingest()` refuses an empty file and a checksum
mismatch.

Pinned by: `tests/test_artifacts.py::TestIngest::test_a_bad_bundle_key_is_refused`, `tests/test_artifacts.py::TestConstraints::test_two_artifacts_cannot_share_a_live_filename`, `tests/test_artifacts.py::TestIngest::test_a_non_sha512_is_refused`, `tests/test_artifacts.py::TestIngest::test_a_traversing_filename_is_reduced_to_a_bare_name`, `tests/test_artifacts.py::TestIngest::test_an_empty_file_is_refused`, `tests/test_artifacts.py::TestIngest::test_version_is_required`

---

## 2. Constraints, lookup and deletion

### 2.1 [unique-live-filename]
**[SPEC]**
`uq_artifact_filename_live` is `UNIQUE(filename) WHERE state IN
('staged','published')`. The push finds an image by filename in one flat
directory, so two live rows must never share a filename.
Pinned by: `tests/test_artifacts.py::TestConstraints::test_two_artifacts_cannot_share_a_live_filename`, `tests/test_artifacts.py::TestConstraints::test_the_database_refuses_a_duplicate_filename_too`

**[NOTE]**
Without this, two uploads could land on one path. Every later hash check
would still pass, because each compares its own row's digest against
whatever bytes are at that path now.

### 2.2 [one-published-image-per-key]
**[SPEC]**
`uq_artifact_bundle_key` is `UNIQUE(platform, bundle_key) WHERE kind =
'image' AND state = 'published'`. `ingest()` and `publish()` check it
first so the user gets a message rather than an `IntegrityError`.
Pinned by: `tests/test_artifacts.py::TestConstraints::test_one_published_artifact_per_bundle_key`, `tests/test_artifacts.py::TestConstraints::test_the_database_refuses_a_duplicate_bundle_key_too`, `tests/test_roles.py::TestArtifactRule::test_the_bundle_key_is_checked_when_it_is_published`

**[NOTE]**
Uniqueness is per `(platform, bundle_key)` among *published* images, so
several `staged` uploads may share a key until one is published.

### 2.3 [requests-name-a-key]
**[SPEC]**
A run request names a bundle key, never a filename or a digest.
`upgrades.resolve_bundle` → `artifacts.get_published` resolves it to a
`published` image row with `bytes_state='present'`, and refuses
anything else.
Pinned by: `tests/test_artifacts.py::TestResolveAndCheck::test_a_request_resolves_a_key_to_a_row`, `tests/test_artifacts.py::TestResolveAndCheck::test_an_unknown_key_is_refused`, `tests/test_artifacts.py::TestResolveAndCheck::test_a_pruned_artifact_is_refused`

**[NOTE]**
A submitted filename and digest could name any bytes against any
checksum, and would bypass the table that owns both.

### 2.4 [delete-refused-while-a-run-needs-it]
**[SPEC]**
`delete()` is one conditional `DELETE`. It is refused while any run in
`pre_checking`, `awaiting_approval` or `running` references the
artifact. A delete removes both the row and the bytes.
Pinned by: `tests/test_artifacts.py::TestDelete::test_a_live_run_blocks_the_delete`, `tests/test_artifacts.py::TestDelete::test_delete_removes_the_row_and_the_bytes`

### 2.5 [finished-runs-keep-their-snapshot]
**[SPEC]**
`upgrade_run_hosts.artifact_id` is `ON DELETE SET NULL`, so a finished
run keeps its snapshot columns and loses only the link. A submit that
races a delete fails on the foreign key and becomes the ordinary "not
registered" refusal.
Pinned by: `tests/test_artifacts.py::TestDelete::test_a_finished_run_keeps_its_snapshot_and_loses_the_link`, `tests/test_artifacts.py::TestDelete::test_the_link_is_a_real_foreign_key`

### 2.6 [check-store-never-guesses]
**[SPEC]**
`check_store()` (`flask --app nethub check-store`, exit 1 on any issue)
re-hashes every artifact that has not been pruned:
- A stale `file_size` is corrected without comment.
- A missing file, or one whose digest has changed, is reported.
- The recorded SHA-512 is never rewritten.

It is a CLI command, never a web route.
Pinned by: `tests/test_artifacts.py::TestResolveAndCheck::test_check_store_reports_altered_bytes_and_never_guesses`, `tests/test_artifacts.py::TestResolveAndCheck::test_check_store_silently_corrects_a_stale_size`, `tests/test_artifacts.py::TestCheckStoreCommand::test_there_is_no_web_route_any_more`

---

## 3. The two-person rule for artifacts

### 3.1 [staged-only-by-the-rule]
**[SPEC]**
An upload lands `published`, unless `two_person_artifacts` binds the
uploader, in which case it lands `staged`. The rule is read twice:
- before streaming, to refuse a key that is already published early;
- after verifying, and that second reading decides.

Pinned by: `tests/test_roles.py::TestArtifactRule::test_with_it_on_an_upload_waits_for_someone_else`, `tests/test_roles.py::TestArtifactRule::test_the_rule_is_read_when_the_upload_is_recorded`

### 3.2 [publish-is-conditional]
**[SPEC]**
- `publish()` refuses the uploader while the rule binds them, and checks
  the bundle key at that moment.
- It is an `UPDATE … WHERE state='staged'` wrapped in a catch for the
  bundle-key `IntegrityError`, which SQLite raises at the UPDATE, not at
  COMMIT.

Pinned by: `tests/test_roles.py::TestArtifactRule::test_the_bundle_key_is_checked_when_it_is_published`, `tests/test_roles.py::TestArtifactRule::test_a_publish_racing_another_under_the_same_key_is_a_message`, `tests/test_roles.py::TestArtifactRule::test_turning_the_rule_off_releases_a_waiting_upload`

### 3.3 [withdraw-uploader-or-admin]
**[SPEC]**
`withdraw()` removes a `staged` upload, row and bytes, and needs no
second person. Only the uploader or an admin may do it. The delete is
conditional on the row still being `staged`, so a withdraw racing a
publish deletes nothing.
Pinned by: `tests/test_roles.py::TestArtifactRule::test_only_the_uploader_or_an_admin_can_withdraw`, `tests/test_roles.py::TestArtifactRule::test_a_withdraw_racing_a_publish_deletes_nothing`

### 3.4 [delete-may-need-a-second-person]
**[SPEC]**
When the rule binds the user, deleting a published artifact is first a
request (`settings.second_person_delete`). A different user's delete then
removes it. The artifact stays usable meanwhile, and two simultaneous
requests make one.
Pinned by: `tests/test_roles.py::TestArtifactRule::test_with_it_on_a_delete_is_a_request_someone_else_confirms`, `tests/test_roles.py::TestArtifactRule::test_two_people_requesting_one_delete_make_one_request`

### 3.5 [artifact-audit-append-only]
**[SPEC]**
- Every upload, publish, withdraw, delete request and delete writes an
  `artifact_audit` row, recording the actor's role.
- The row copies the artifact's id, key, filename and digest instead of
  holding a foreign key.
- Triggers make the table append-only, and `artifacts` uses
  `AUTOINCREMENT`, so a deleted id is never reused.

Pinned by: `tests/test_roles.py::TestArtifactRule::test_the_artifact_audit_is_append_only`, `tests/test_roles.py::TestArtifactRule::test_a_deleted_artifacts_id_is_never_reused`, `tests/test_roles.py::TestArtifactRule::test_an_admin_acts_alone_and_the_record_says_so`

---

## 4. How it works

**[SPEC]**
| route | method | does |
|---|---|---|
| `/artifacts` | GET | list |
| `/artifacts/new` | GET, POST | upload form; POST is `ingest()` |
| `/artifacts/<id>/publish` | POST | `publish()` a staged upload |
| `/artifacts/<id>/delete` | POST | needs `confirm=yes`; `withdraw()` if staged, else `delete()` |

- `ARTIFACT_STORE` (default `<repo>/instance/artifacts`) is one flat
  directory that NetHub owns.
- The sibling never reads `ARTIFACT_STORE`; it pushes from
  `NETHUB_SEARCH_DIR`. The Quadlet units set both to `/app/artifacts`, and
  they must name the same directory.
- Any request over `MAX_CONTENT_LENGTH` (1500 MiB) gets an app-wide 413
  page that states the limit.
- The routes only ever create `kind='image'`, `platform='iosxe'`
  artifacts.

Pinned by: `tests/test_errors.py::test_413_states_the_configured_limit`

**[SPEC]**
The SHA-512 is computed once at ingest and then read three times without
being recomputed:
1. snapshotted onto `upgrade_run_hosts` at submit;
2. compared with the store before stage (`phases.check_source`);
3. checked by the device's `verify /sha512` after the push and before
   activate.

---

## 5. Decisions

**[NOTE]**
- **SHA-512 is the only hash algorithm, and there is no `hash_algo`
  column.**
  - Why: IOS-XE's `verify /sha512` fixes the algorithm at the device end.
    A second algorithm would mean storing two digests or recomputing one
    at egress.
  - Reopen if: a platform arrives that supports only another algorithm.
    That would be an *added* algorithm, with the column.
- **MD5 was considered and rejected.** Three findings:
  - It saves no code. Netmiko's `compare_md5` compares NetHub's own file
    against the device, but the check needed is the digest recorded at
    ingest against the device.
  - The speed gain is small: 18.5 s against 33.9 s per 408 MB verify, so
    about 1.5 min per device across an upgrade.
  - Chosen-prefix collisions, practical since 2019, let whoever supplies
    an image hand over a benign file that collides with a malicious one.
    Plain-HTTP day-0 and any future pull transport have nothing else
    behind the digest. (Swapping bytes to match a digest *already*
    recorded is a preimage attack, which MD5 still resists, so "MD5 is
    broken" alone is the weaker argument.)
- **NetHub is the only source of image bytes, with no mirror and no
  second store.**
  - Why: provenance is never a question.
  - Cost: every byte crosses the link from NetHub to the device. See
    future.md (distributed distribution).
- **There is no supersede flow.** Delete is a hard removal. Nothing
  writes `superseded`, `pruned` or `superseded_by_id`; don't add handling
  for them without a flow that reads them.
- **There is no rendered registry file, publish job or lock.** The table
  plus the two partial unique indexes are the whole mechanism.

---

## 6. Pitfalls

**[BUG] One image's digest recorded against another's bytes**
- Symptom: `check-store` reports a digest mismatch for a freshly
  uploaded image, and stage fails `checksum`.
- Cause: two concurrent uploads of the same filename, where the final
  move overwrites.
- Fix: rule 1.3 (`os.link`) and rule 1.4.

---

## 7. Known gaps

**[SPEC]**
- `kind` values `script` and `config`, the state `superseded`, and
  `bytes_state='pruned'` exist in the schema, but nothing writes them.
- `withdraw` and `delete` remove the file only after the commit. A crash
  in between leaves an orphan file, which `check_store` does not report
  because it walks rows; the orphan blocks that filename (rule 1.5).
- There is no retention purge. Artifact bytes and rows stay until someone
  deletes them (future.md).
- A pending delete request cannot be withdrawn and never expires.
- Weak pins: 1.1 (chunking and the temp file are untested) and 2.4 (that
  it is one statement is untested).
- Nothing makes a Flask-side compromise that rewrites `artifacts.sha512`
  detectable. `check_store` catches the file drifting from its row, not a
  row that lies consistently.
