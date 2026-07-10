# Contributing

## Branching

This project is built milestone-by-milestone. Branch off `main` per
milestone (or per sub-task within a milestone):

```
feature/milestone-0-repo-foundation
feature/milestone-1-ingestion
...
```

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add job execution service
fix: sanitize database health check error messages
docs: document migration rollback procedure
chore: bump sqlalchemy version
```

## Before pushing

Run, and make sure all pass:

```bash
make test
uv run ruff format --check .
uv run ruff check .
uv run mypy app apps packages
```

If Docker is available locally, also run the integration suite:

```bash
docker compose up -d postgres
make migrate
make test-integration
```

## Pull requests

Use the PR template (`.github/pull_request_template.md`) — fill in every
section, particularly "Known limitations" and "Rollback plan". The
`ci-success` check in `.github/workflows/ci.yml` must pass before merging.
