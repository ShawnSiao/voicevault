# VoiceVault data-safety skill

Use this skill when working on collection, import, export, evidence, storage, browser/session handling, documentation about data policy, or fixtures that represent public content.

## Workflow

1. Read `AGENTS.md`, `.agent/instructions/data-boundaries.md`, and `docs/DATA-POLICY.md`.
2. Confirm the change does not commit operator data, credentials, cookies, browser profiles, imported posts, generated indexes, or evidence stores.
3. Keep collection and platform access explicit, task-based, and operator initiated.
4. Use synthetic fixtures unless provenance and license constraints are documented.
5. Update policy or README documentation when the data lifecycle changes.

## Review checklist

- Does the change preserve person-scoped attribution?
- Are source URLs, timestamps, revisions, and evidence review paths preserved where relevant?
- Can the behavior be tested offline with fixtures or fakes?
- Is deletion/export behavior documented if it changed?
