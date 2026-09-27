## What changed and why

## How it was verified

- [ ] `uv run pytest` passes on SQLite (and on PostgreSQL for model, query or migration changes)
- [ ] `uv run ruff check app tests sdk` passes
- [ ] Docs updated for any behaviour change; `docs/openapi.json` regenerated for API changes
- [ ] New operator actions exist in the service, the CLI and the portal (see AGENTS.md)
