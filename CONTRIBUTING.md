# Contributing

1. Keep runtime data, browser artifacts, credentials, and third-party corpora out of commits.
2. Add or update tests for behavior changes.
3. Run `python -m unittest discover -s tests -t . -v` before opening a pull request.
4. Keep platform access outside automated tests; use fixtures or fake bridge executors.
5. State any data provenance and license constraints for new examples or fixtures.
