# VoiceVault release-checks skill

Use this skill when changing version metadata, release helpers, distribution behavior, or release documentation.

## Workflow

1. Read `AGENTS.md` and inspect `pyproject.toml`, `src/voicevault/release*.py`, and release-related tests.
2. Keep version changes consistent across metadata and tests.
3. Run the focused release tests before the full suite:
   `python -m unittest tests.test_release -v`
4. Confirm release commands do not include runtime data, credentials, imported content, or generated indexes.

## Notes

Release tooling should remain deterministic and should not require live platform access for tests.
