# Data boundary instructions

VoiceVault distributes software, not a public-content dataset. A fresh workspace starts empty except for bundled synthetic samples used for exploration and compatibility tests.

Do not commit:

- Imported posts or third-party corpora.
- Runtime databases, evidence stores, generated indexes, vector stores, or collection output.
- Browser profiles, cookies, credentials, tokens, or API keys.
- Screenshots or exports containing third-party or personal content.
- Operator task handoff files that may reveal local source state.

When adding fixtures, make them synthetic, small, deterministic, and license-safe. When adding integrations, keep platform access explicit and user initiated.
