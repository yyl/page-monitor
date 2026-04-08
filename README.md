# Site update tracker

This repo checks configured pages for updates and posts to Discord when something changes.

## Get started

Local:

```bash
uv run python -m unittest discover -s tests
uv run python scripts/check_updates.py --state-path .cache/tracker/state.json --dry-run
```

Local with real Discord notifications:

```bash
export DISCORD_WEBHOOK_URL="your-webhook-url"
uv run python scripts/check_updates.py --state-path .cache/tracker/state.json
```

GitHub:

1. Push this repo to GitHub.
2. Add a repository secret named `DISCORD_WEBHOOK_URL`.
3. The scheduled workflow in `.github/workflows/track-updates.yml` will run every 6 hours.
4. The test workflow in `.github/workflows/test.yml` runs on every push and pull request.

## File structure

- `config/tracker.json`: list of tracked targets. Each target includes a `type`, `url`, and optional `name`.
- `pyproject.toml`: project metadata and Python dependencies managed by `uv`.
- `scripts/check_updates.py`: main checker script. Fetches pages, parses the latest update info, compares against saved state, and sends Discord notifications.
- `tests/test_check_updates.py`: parser tests.
- `.github/workflows/track-updates.yml`: scheduled GitHub Action that restores state, runs the checker, and saves updated state.
- `.github/workflows/test.yml`: GitHub Action that runs tests on pushes and pull requests.
- `.cache/tracker/state.json`: last seen state for each tracked URL. Created locally by the script and restored/saved in GitHub Actions via cache.

## How it works

1. The script reads `config/tracker.json`.
2. For each target, it fetches the page and parses the latest updated date, issue title, and issue link.
3. It compares the latest values with `.cache/tracker/state.json`.
4. On the first run, it seeds the state and sends no notification.
5. On later runs, if any target changed, it sends one Discord message listing the updates.
6. In GitHub Actions, the state file is restored from cache at the start of a scheduled run and saved back after a successful run.
