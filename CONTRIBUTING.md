# Contributing

VoiceVault accepts code changes through the public repository only. Runtime data,
private archives, and collection experiments remain local and are never merged or
copied into this repository.

## Change workflow

1. Create a topic branch from the latest `main`.
2. Keep runtime data, browser artifacts, credentials, and third-party corpora out of commits.
3. Add or update tests for behavior changes.
4. Run `python -m unittest discover -s tests -t . -v` before opening a pull request.
5. Open a pull request against `main`; do not push changes directly to `main`.
6. Merge only after the required Windows, macOS, and Linux CI checks pass.

## Data boundary

- Keep platform access outside automated tests; use synthetic fixtures or fake bridge executors.
- State provenance and license constraints for every new example or fixture.
- Do not merge, mirror, or cherry-pick data-bearing history from a private archive.
- Before pushing, inspect the staged paths and scan for credentials, account identifiers, and corpus content.
