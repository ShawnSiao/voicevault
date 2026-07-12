# Repository instructions

## Architecture overview

VoiceVault is organized as a Python package under `src/voicevault` with a standard-library-first implementation. The CLI entry point is `voicevault.cli:main`; HTTP routing and UI support live in server, routing, API, dashboard, and UI modules. Person archive, post archive, collection, indexing, retrieval, question, and answer modules are separated so tests can exercise them independently.

## Testing expectations

- Use `python -m unittest discover -s tests -t . -v` for the full suite.
- Use `python -m unittest tests.<module> -v` for focused checks.
- Keep tests deterministic and offline.
- Prefer temporary directories for workspace, data-dir, and knowledge-base state.

## Documentation expectations

- Update `README.md` and `README.zh-CN.md` together when changing operator-facing setup or workflows.
- Update `docs/DATA-POLICY.md` when data collection, storage, export, or deletion semantics change.
- Keep examples synthetic unless licensing and provenance are explicit.
