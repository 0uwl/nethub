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
      vars.yml                (software_registry projection)
```

## Why the split

The original example had one inventory tree carrying everything, which
put three things in a user-writable file that must not be there:

- **`software_registry`.** Design doc §7.2 makes NetHub the sole writer of the
  registry and says the `artifacts` table wins on any inconsistency. An
  uploaded catalog lets a user name any filename and any SHA-512, which
  bypasses the table entirely and breaks 3.4's "hash computed once at
  ingest, consumed three times" at the third consumption.
- **`ansible_user`.** Once this is the submitter's own device username,
  a user who can set it can run as anyone, and the device-side log
  stops being evidence.
- **Jinja.** `software_bundle: "{{ software_registry['...'] }}"` is a template
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

`image_transport` is on that list too, and for a sharper reason than the
rest: it selects which credential the transfer uses (§4.3.1), so a
submitter who could set it could choose to have the deployment's
distribution credential spent instead of their own. It is deployment-level,
rendered into `group_vars/all.yml` from the `settings` table.

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
- **`scp_pass` riding on `ansible_user`, and `vault.yml` with it.** Under
  the push transport there is no second, distribution-specific account or
  password to render at all — the image travels over the same credential
  `ansible_user` already authenticates (§4.3.1). The pull transport does
  need a distribution account, but it still renders no password here:
  `distribution_user` is a var, the password is injected per execution
  (§9.1) and never lands in the `private_data_dir`'s inventory. A vault
  would only re-add a key to dispose of, which is why it stayed gone. See
  the comment in `rendered/group_vars/all.yml`.
- **`ansible_become` / `ansible_become_method`.** NetHub requires
  privilege 15 at login (§4.3), so there is no escalation step and no
  `become_password` to collect. See the comment in
  `rendered/group_vars/all.yml`.
