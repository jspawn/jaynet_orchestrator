# Upgrading JayNet

How a deploy moves from one version to the next, and what migrates on its
own. Applies to the reference layout (install root `$JAYNET_HOME`, data in
`$JAYNET_DATA`, systemd `--user` services).

## Procedure

```bash
cd /srv/orchestrator            # the live checkout
git pull                        # or: git fetch && git checkout vX.Y.Z
systemctl --user restart jaynet-web litellm-proxy
```

Then check **Admin → Status** (services up, version tile shows the new
number) and run a short chat turn.

If the checkout has local edits you want to keep, `git stash` before the
pull and `git stash pop` after.

## Renamed in 0.9.x: orchestrator → jaynet

The deployment-facing names were rebranded so a JayNet install is
recognizable next to other software:

| old | new |
|---|---|
| `~/.config/orchestrator.env` | `~/.config/jaynet.env` |
| `orchestrator-web.service` | `jaynet-web.service` |
| `ORCH_*` env vars | `JAYNET_*` (ORCH_* still read as a fallback) |

The **Python code dual-reads** both prefixes, so an old env file keeps
working with new code — but the **systemd units don't**: they substitute
`${JAYNET_*}` from the env file directly. To migrate an existing install:

```bash
cd /srv/orchestrator && git pull
# 1. env file: new name, JAYNET_* keys (keeps every value)
sed -e 's/^ORCH_/JAYNET_/' -e 's/^#ORCH_/#JAYNET_/' -e 's/^# ORCH_/# JAYNET_/' \
    ~/.config/orchestrator.env > ~/.config/jaynet.env && chmod 600 ~/.config/jaynet.env
# 2. units: swap the web unit, refresh the proxy unit
cp systemd/jaynet-web.service systemd/litellm-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now orchestrator-web
systemctl --user enable --now jaynet-web litellm-proxy
# 3. when everything runs: rm ~/.config/orchestrator.env
```

## What migrates automatically

- **SQLite schemas** (`$JAYNET_DATA/*.db`): every store runs
  `CREATE TABLE IF NOT EXISTS` plus additive `ALTER TABLE … ADD COLUMN`
  migrations on service start. Booting the new version upgrades the DBs in
  place — no manual step. Migrations are **additive only**: old code can
  still open a newer DB, so a rollback (`git checkout <old tag>` + restart)
  is safe.
- **Preset catalog** (`presets.db`): seeded from `runtime.yaml` on first
  boot; afterwards it's admin-managed in the UI and untouched by upgrades.
- **Custom layer** (`$JAYNET_DATA/custom/`): Studio-created skills, chains,
  tools and connectors live outside the git tree — pulls never touch them.

## What you must check yourself

- **`config/runtime.yaml` is tracked in git.** A pull brings new/changed
  keys and may conflict with local edits (`git stash` flow above). Skim
  `git diff <old>..<new> -- config/runtime.yaml` after pulling; new keys
  ship with comments explaining themselves.
- **New Python dependencies**: compare `requirements*.txt` changes in the
  pull and reinstall into the venv(s) if they moved:
  `uv pip install --python .venv/bin/python -r requirements.txt …`
- **Breaking changes** are listed in `CHANGELOG.md` under the version —
  read it before upgrading across a minor bump (0.9 → 0.10), not just
  patches.

## Data safety

Everything that matters lives in `$JAYNET_DATA` (chats, users, projects,
wiki, memory, trace, uploads, custom layer). Back it up before a version
jump you're unsure about:

```bash
systemctl --user stop jaynet-web
tar -C /srv -czf jaynet-data-backup-$(date +%F).tgz data
systemctl --user start jaynet-web
```
