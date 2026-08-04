# Upgrading JayNet

How a deploy moves from one version to the next, and what migrates on its
own. Applies to the reference layout (install root `$ORCH_HOME`, data in
`$ORCH_DATA`, systemd `--user` services).

## Procedure

```bash
cd /srv/orchestrator            # the live checkout
git pull                        # or: git fetch && git checkout vX.Y.Z
systemctl --user restart orchestrator-web litellm-proxy
```

Then check **Admin → Status** (services up, version tile shows the new
number) and run a short chat turn.

If the checkout has local edits you want to keep, `git stash` before the
pull and `git stash pop` after.

## What migrates automatically

- **SQLite schemas** (`$ORCH_DATA/*.db`): every store runs
  `CREATE TABLE IF NOT EXISTS` plus additive `ALTER TABLE … ADD COLUMN`
  migrations on service start. Booting the new version upgrades the DBs in
  place — no manual step. Migrations are **additive only**: old code can
  still open a newer DB, so a rollback (`git checkout <old tag>` + restart)
  is safe.
- **Preset catalog** (`presets.db`): seeded from `runtime.yaml` on first
  boot; afterwards it's admin-managed in the UI and untouched by upgrades.
- **Custom layer** (`$ORCH_DATA/custom/`): Studio-created skills, chains,
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

Everything that matters lives in `$ORCH_DATA` (chats, users, projects,
wiki, memory, trace, uploads, custom layer). Back it up before a version
jump you're unsure about:

```bash
systemctl --user stop orchestrator-web
tar -C /srv -czf orch-data-backup-$(date +%F).tgz data
systemctl --user start orchestrator-web
```
