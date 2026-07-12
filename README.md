# VoiceVault

[简体中文](README.zh-CN.md)

VoiceVault is a local-first, single-user archive and evidence-grounded question-answering tool for public content. It treats a person as the knowledge-base boundary, keeps platform accounts and post revisions attributable, and returns answers with citations and uncertainty notes.

VoiceVault distributes software, not a public-content dataset. A fresh person-archive workspace starts empty; imported posts, indexes, evidence, browser sessions, and credentials stay on the operator's machine. `voicevault init` also creates local schema files and bundled synthetic legacy samples for exploration.

## Included capabilities

- Create people and bind one or more platform accounts to each person.
- Create local collection tasks with date ranges, coverage checks, progress, recovery actions, and a 30-minute task lease.
- Preserve posts, revisions, timestamps, source URLs, collection observations, and reviewable evidence.
- Import a completed historical Xueqiu JSON archive after validation, deduplication, and binding to an existing person account.
- Build person-scoped knowledge-base indexes using local full-text retrieval, with optional vector retrieval when an embedding provider is configured.
- Ask questions against selected people, review answer status, citations, evidence, and uncertainty.
- Use the local HTTP UI, JSON APIs, and standard-library CLI.
- Keep the established Role/Statement and release-analysis commands available as legacy functionality.

## Requirements

- Git
- Python 3.11 or later
- Internet access only for installing Python packages or accessing a collection source that the operator is authorized to use

## Install and run on Windows

Open PowerShell:

```powershell
git clone https://github.com/ShawnSiao/voicevault.git
Set-Location voicevault

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e .

$root = (Get-Location).Path
$kb = Join-Path $root 'knowledge-base'
$env:VOICEVAULT_DATA_DIR = Join-Path $env:LOCALAPPDATA 'VoiceVault'

voicevault init --kb $kb
voicevault serve --kb $kb --root $root --data-dir $env:VOICEVAULT_DATA_DIR
```

If PowerShell execution policy prevents activation, run the virtual-environment executable directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\voicevault.exe --version
```

## Install and run on macOS

Open Terminal:

```bash
git clone https://github.com/ShawnSiao/voicevault.git
cd voicevault

python3.11 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .

ROOT="$(pwd)"
KB="$ROOT/knowledge-base"
export VOICEVAULT_DATA_DIR="$HOME/Library/Application Support/VoiceVault"

voicevault init --kb "$KB"
voicevault serve --kb "$KB" --root "$ROOT" --data-dir "$VOICEVAULT_DATA_DIR"
```

Use `python3` instead of `python3.11` only when it reports Python 3.11 or later.

## Open the local service

The server binds to `127.0.0.1` by default and prints its URL. Open:

```text
http://127.0.0.1:8765/
```

The person-archive workspace is empty on the first visit. The initialized legacy knowledge base also contains bundled synthetic samples. The normal archive flow is:

```text
Create person → bind account → create collection task or import an archive
→ build the knowledge base → ask a question → inspect evidence
```

VoiceVault does not read browser cookies and does not automatically access external platforms. Collection is explicitly task-based and must follow the source platform's terms and applicable law.

## Check service capabilities

Windows PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/status
Invoke-RestMethod http://127.0.0.1:8765/api/capabilities
Invoke-RestMethod http://127.0.0.1:8765/api/workspace
```

macOS Terminal:

```bash
curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8765/api/capabilities
curl http://127.0.0.1:8765/api/workspace
```

Inspect CLI command groups:

```bash
voicevault --help
voicevault archive import --help
voicevault collection --help
voicevault question --help
```

## Reuse an existing local runtime

Pass the existing runtime *directory* through `--data-dir`; do not copy a database into the repository. For example, on Windows:

```powershell
$env:VOICEVAULT_DATA_DIR = 'W:\VoiceVault'
voicevault serve --kb $kb --root $root --data-dir $env:VOICEVAULT_DATA_DIR
```

`--kb` is a knowledge-base directory and `--data-dir` is the runtime-data directory. Both are local state and must remain outside version control.

## Development

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s tests -t . -v
```

The same command works in macOS Terminal after activation:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -t . -v
```

## Data and security

Read [the data policy](docs/DATA-POLICY.md) before importing content. Do not commit imported posts, account archives, indexes, evidence, exports, screenshots, browser profiles, cookies, credentials, or task handoff material.

Security issues should be reported according to [SECURITY.md](SECURITY.md). Contribution expectations are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

The original code in this repository is released under the [MIT License](LICENSE). The license does not grant rights to third-party content, platform data, trademarks, or user submissions.
