# VoiceVault

VoiceVault is a local-first, single-user archive and question-answering tool for public content. It groups platform accounts by person, preserves post revisions and collection evidence, and answers questions with attributable citations.

## What this repository contains

- The Python application, local HTTP UI, SQLite schema, tests, and UI design assets.
- No production database, browser profile, authentication material, captured task exchange, or real-person archive.
- No built-in third-party corpus. Import only content you are entitled to collect, retain, and process.

## Quick start

Requirements: Python 3.11 or later.

```powershell
python -m pip install -e .
$env:VOICEVAULT_DATA_DIR = "$env:LOCALAPPDATA\VoiceVault"
voicevault serve --kb "$PWD\knowledge-base" --root "$PWD" --data-dir $env:VOICEVAULT_DATA_DIR
```

Open the loopback URL printed by the server. The UI does not read browser cookies or call external platforms on its own. Collection is explicitly task-based and must follow the source platform's terms and applicable law.

## Development

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s tests -t . -v
```

## Data and safety

Read [docs/DATA-POLICY.md](docs/DATA-POLICY.md) before importing public content. Runtime files, indexes, evidence, exports, and browser artifacts must remain local and are excluded from version control.

## License

The original code in this repository is released under the [MIT License](LICENSE). The license does not grant rights to third-party content, platform data, trademarks, or user submissions.
