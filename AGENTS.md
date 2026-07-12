# VoiceVault Agent Guide

This file provides project-level instructions for AI agents working in this repository. It applies to the entire repository unless a more specific `AGENTS.md` file is added in a child directory.

## Project snapshot

VoiceVault is a local-first, single-user archive and evidence-grounded question-answering tool for public content. The project keeps public content scoped by person, preserves platform accounts and post revisions, and answers questions with citations and uncertainty notes.

The repository ships software only. Imported posts, generated indexes, evidence stores, browser sessions, credentials, screenshots, exports, and task handoff artifacts are local runtime data and must not be committed.

## Repository map

- `src/voicevault/`: Python package for the CLI, local HTTP server, APIs, archive services, retrieval, indexing, release tooling, and legacy Role/Statement features.
- `tests/`: `unittest` test suite covering CLI, APIs, archive workflows, retrieval, indexing, collection jobs, UI rendering, and release helpers.
- `docs/`: Project policy and operator-facing documentation, including data policy.
- `.agent/`: Project-specific AI agent knowledge, reusable skills, and workflow instructions.
- `README.md` and `README.zh-CN.md`: User-facing setup and operating guides in English and Simplified Chinese.

## Development environment

- Use Python 3.11 or later.
- Prefer standard-library tools already used by the project; the package currently has a minimal runtime dependency set.
- Install locally with `python -m pip install -e .` when you need to run the `voicevault` console script.
- Keep `PYTHONDONTWRITEBYTECODE=1` when running tests if you want to avoid writing `__pycache__` files.

## Common commands

```bash
python -m unittest discover -s tests -t . -v
python -m unittest tests.test_cli -v
python -m unittest tests.test_server -v
python -m unittest tests.test_person_archive_e2e -v
voicevault --help
```

## Coding guidelines

- Do not wrap imports in `try`/`except` blocks.
- Prefer explicit dataclasses, typed function signatures, and small pure helper functions for domain logic.
- Keep local-first behavior intact: features should work without a network unless the operator explicitly configures an external source or provider.
- Tests must not access live platforms. Use fixtures, temporary directories, fake bridge executors, or in-memory services.
- Preserve evidence and citation semantics when changing answer, retrieval, archive, or index code.
- Do not introduce hidden background collection or automatic browser-cookie access.
- Keep user-facing text bilingual where the surrounding feature already has English and Simplified Chinese documentation.

## Data and security boundaries

- Never commit credentials, cookies, browser profiles, personal archives, imported posts, generated databases, vector indexes, local evidence stores, screenshots, or exported third-party content.
- Treat `knowledge-base`, `VOICEVAULT_DATA_DIR`, runtime databases, and collection output as local operator state.
- New examples and fixtures must be synthetic or clearly licensed for repository use.
- Follow `docs/DATA-POLICY.md`, `SECURITY.md`, and `CONTRIBUTING.md` before changing collection, import, export, or storage behavior.

## Agent workflow

1. Read this file and the relevant `.agent/instructions/*.md` or `.agent/skills/*/SKILL.md` files before editing.
2. Inspect the existing tests closest to the code you plan to change.
3. Make the smallest coherent change and update tests or docs together with behavior changes.
4. Run focused tests first, then the full test suite when practical.
5. In final notes, call out any test that could not run and the concrete environment limitation.
