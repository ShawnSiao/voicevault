## Summary

- Describe the change and its user or developer impact.

## Validation

- [ ] `python -m unittest discover -s tests -t . -v`
- [ ] Installation or service smoke test when startup behavior changes
- [ ] Windows, macOS, and Linux CI checks pass

## Data boundary

- [ ] No runtime database, imported post, account archive, evidence, credential, cookie, browser artifact, or real-person content is included
- [ ] New fixtures are synthetic or explicitly licensed, with provenance stated
