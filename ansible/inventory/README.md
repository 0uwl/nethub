# Example inventory layout

Sketch of how NetHub gets an inventory in front of the upgrade
playbooks (`ansible/playbooks/stage_cisco_upgrade.yml` and
`install_cisco_upgrade.yml`). One file the user owns; everything else
NetHub renders. The boundary between them is the point.

```
upgrade_request.example.yml   <- the user uploads this
hosts.yml                     <- NetHub writes this, per job
upgrade_batch.yml             <- NetHub writes this, per job
group_vars/
  all.yml
  os_iosxe.yml                (software_registry projection)
  os_junos.yml                (illustrates an adopted-but-empty registry --
                               see "What went away" below)
```

Everything but `upgrade_request.example.yml` illustrates NetHub's
*output* — there's no `rendered/` subdirectory marking that boundary
anymore, just the `.example.yml` suffix on the one file that's actually
a submitted request rather than something NetHub produced.

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

- **The master fleet inventory.** 8 calls for "a minimal per-job ...
  inventory rather than the full fleet inventory", and 2 rules out
  being an inventory manager. `hosts.yml` here is that minimal per-job
  rendering (two illustrative hosts), not a persistent fleet inventory
  NetHub maintains — hosts arrive with a request and leave with the job.
- **`group_vars/junos.yml` as upgrade support.** Out of vendor scope
  (2.1) — NetHub does not dispatch upgrades for it, and
  `group_vars/os_junos.yml`'s own header comment says so. It's kept in
  this example tree for a different reason than the original bullet
  had: it illustrates the *adopted-but-empty* registry case (a file
  with no `software_registry` key yet, which NetHub writes one into on
  adoption) rather than a rendered projection of real entries. The
  platform-keyed naming (`os_<platform>.yml`) is the same shape a real
  second platform would use, so adding one back is still an addition,
  not a rework.
- **`scp_pass` riding on `ansible_user`, and `vault.yml` with it.** Under
  the push transport there is no second, distribution-specific account or
  password to render at all — the image travels over the same credential
  `ansible_user` already authenticates (§4.3.1). The pull transport does
  need a distribution account, but it still renders no password here:
  `distribution_user` is a var, the password is injected per execution
  (§9.1) and never lands in the `private_data_dir`'s inventory. A vault
  would only re-add a key to dispose of, which is why it stayed gone. See
  the comment in `group_vars/all.yml`.
- **`ansible_become` / `ansible_become_method`.** NetHub requires
  privilege 15 at login (§4.3), so there is no escalation step and no
  `become_password` to collect. See the comment in `group_vars/all.yml`.
