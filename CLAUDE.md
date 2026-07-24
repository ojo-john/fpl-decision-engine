# Project instructions for agents working in this repo

## Keep ARCHITECTURE.md current — this is not optional

`ARCHITECTURE.md` at the repo root is the ground-truth map of this system,
built by reading the actual code rather than trusting existing docs (most of
which — `README.md`, `docs/`, several `app/*/__init__.py` files — were found
stale, still describing early-milestone placeholders for packages that are
now fully built).

**Any agent that changes something ARCHITECTURE.md describes must update it
in the same session, before calling the work done.** This includes but is
not limited to: adding/removing a `JobType` or its handler, adding/removing
an API route, adding a new ingestion pipeline or data source, changing the
optimization gate's logic, adding/removing a database table or a
load-bearing FK relationship, adding a new frontend page or test suite,
changing a CI/scheduling workflow. If a change doesn't obviously map to an
existing section, add one rather than skipping the update.

Do not patch ARCHITECTURE.md from memory or from a commit message — reverify
the specific claim against the real code first, the same way the document
was originally built (see its own "Methodology" section at the bottom).

## Don't trust a docstring's claim about what does/doesn't exist

This codebase has a demonstrated pattern of stale module-level docstrings
(`app/*/__init__.py` files literally reading "Placeholder — implemented in a
later milestone" for packages that are fully built; the `JobType` enum's own
docstring claiming only one handler exists when 16 of 18 do). Before stating
that something is unbuilt, unused, or a stub, check the real code (e.g.
`app/jobs/registry.py`'s actual `@register_handler` calls, actual router
decorators in `apps/api/routers/`), not a comment describing it.
