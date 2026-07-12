# VoiceVault development skill

Use this skill when changing application code, CLI behavior, APIs, UI rendering, indexing, retrieval, archives, or tests in this repository.

## Workflow

1. Read `AGENTS.md` and `.agent/instructions/repository.md`.
2. Identify the affected module under `src/voicevault` and the closest tests under `tests/`.
3. Preserve local-first behavior and deterministic offline tests.
4. Add or update focused tests for behavior changes.
5. Run a focused `python -m unittest tests.<module> -v` command, then run `python -m unittest discover -s tests -t . -v` when practical.

## Project reminders

- The package supports Python 3.11+.
- Do not put `try`/`except` around imports.
- Keep generated/runtime data out of the repository.
- Do not let tests call live external platforms.
