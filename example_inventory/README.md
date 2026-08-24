# Example inventory layout

Sketch of how NetHub gets an inventory in front of `upgrade_iosxe.yml`.
Two files, two owners, and the boundary between them is the point.

```
upgrade_request.example.yml   <- the user uploads this
rendered/                     <- NetHub writes this, per job
  hosts.yml
  group_vars/
    all.yml
    iosxe/
      vars.yml                (image_registry projection)
      vault.yml               (scp_pass, deployment secret)
```

## Why the split

The original example had one inventory tree carrying everything, which
put three things in a user-writable file that must not be there:

- **`image_registry`.** README 7.2 makes NetHub the sole writer of the
  registry and says the `artifacts` table wins on any inconsistency. An
  uploaded catalog lets a user name any filename and any SHA-512, which
  bypasses the table entirely and breaks 3.4's "hash computed once at
  ingest, consumed three times" at the third consumption.
- **`ansible_user`.** Once this is the submitter's own device username,
  a user who can set it can run as anyone, and the device-side log
  stops being evidence.
- **Jinja.** `image_bundle: "{{ image_registry['...'] }}"` is a template
  expression. Accepting user-authored Jinja into an EE run is the
  playbook-upload hole wearing a different hat.

So the user's file stops being an Ansible inventory and becomes a
*request document*: hosts, a bundle key per host, and a short list of
typed knobs. NetHub validates it and compiles the real inventory.

## What a request may set

| Field | Notes |
|---|---|
| `platform` | Must be supported (2.1). `iosxe` only today. |
| `hosts[].name` | Inventory hostname. |
| `hosts[].ansible_host` | Address. |
| `hosts[].bundle` | Bare key, resolved against `artifacts`. Must be `published`. |
| `hosts[].flash_dir` | Optional. `flash:` / `bootflash:`. |

Anything else is rejected at submit time. Connection vars, credentials,
and registry entries are NetHub's to write.

Beyond this set, the answer is a pull request against the curated
playbook -- code review -- not a runtime upload.

## What went away

- **The master fleet inventory** (`hosts.yml` in the original). 8 calls
  for "a minimal per-job ... inventory rather than the full fleet
  inventory", and 2 rules out being an inventory manager. Hosts arrive
  with a request and leave with the job.
- **`group_vars/junos.yml`.** Out of vendor scope (2.1). The
  platform-keyed directory shape is kept so adding it back is an
  addition, not a rework.
- **`scp_pass` riding on `ansible_user`.** Now a separate `scp_user`.
  See the comment in `rendered/group_vars/iosxe/vars.yml`.
