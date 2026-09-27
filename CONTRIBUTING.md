# Contributing

Thanks for helping improve Custom Domain. Bug reports, documentation fixes
and pull requests are all welcome.

## Before you start

- **Questions and bugs:** open an issue with the bug report form. Include the
  version (the tag of the image you run, `CUSTOM_DOMAIN_IMAGE` in
  `deploy/.env`), what you did,
  what you expected and what happened; `custom-domain doctor` output helps.
- **Security problems:** do not open an issue. Report them privately as
  described in [SECURITY.md](SECURITY.md).
- **Larger changes:** open an issue first to agree on the approach, so the
  pull request is not wasted work.

## Making a change

[AGENTS.md](AGENTS.md) is the working guide for this repository, for people
and coding agents alike. In short:

```bash
uv sync --all-packages
uv run pytest                       # SQLite; set TEST_DATABASE_URL for PostgreSQL
uv run ruff check app tests sdk
uv run ruff format <files you changed>
```

- Keep one change per pull request, with tests, and update the document that
  describes the behaviour you changed.
- Changes to the v1 API regenerate `docs/openapi.json`
  (`uv run custom-domain openapi export --output docs/openapi.json`).
- New migrations must stay backward compatible for one release.
- Read the "Rules the code depends on" section of AGENTS.md before touching
  routing, the edge configuration, credentials or the portal.

CI runs the linter and the full suite on SQLite and PostgreSQL with a real
Caddy; a pull request is reviewed once it is green.

## License

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), like the rest of the project.
