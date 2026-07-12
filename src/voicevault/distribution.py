from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from . import __version__
from .checksums import file_sha256, write_sha256_file

DIST_MANIFEST_SCHEMA_VERSION = 1
PACKAGE_NAME = f"voicevault-cli-v{__version__}"
ROOT_FILES = ("pyproject.toml", "README.md", "AGENTS.md")
INCLUDE_DIRS = (
    "src/voicevault",
    "docs/integration",
    "docs/product",
    "docs/release",
    "examples",
)
EXCLUDED_DIR_NAMES = {
    ".cache",
    ".git",
    ".temp",
    ".venv",
    ".voicevault",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo"}


def write_distribution_package(repo_root: Path, out_dir: Path | None = None) -> dict[str, Any]:
    root = repo_root.resolve()
    _validate_repo_root(root)
    target_dir = (out_dir or root / "dist").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    package_name = f"voicevault-cli-v{__version__}"
    zip_path = target_dir / f"{package_name}.zip"
    manifest_path = target_dir / f"{package_name}-manifest.json"
    install_path = target_dir / f"{package_name}-INSTALL.md"

    package_files = _collect_package_files(root)
    install_markdown = _install_markdown()
    package_manifest = {
        "schema_version": DIST_MANIFEST_SCHEMA_VERSION,
        "product": {
            "chinese_name": "声迹",
            "english_name": "VoiceVault",
            "repository": "public-voice-archive",
            "version": __version__,
        },
        "package_name": package_name,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "entry_points": ["python -m voicevault", "voicevault"],
        "files": [_relative_archive_name(root, path) for path in package_files],
        "generated_files": ["INSTALL.md", "distribution-manifest.json"],
        "data_boundary": [
            "No private knowledge-base content is packaged.",
            "No .voicevault state, credentials, cookies, platform caches, audio samples, or build caches are packaged.",
        ],
    }
    _write_distribution_zip(root, package_files, zip_path, package_name, install_markdown, package_manifest)
    zip_sha256 = file_sha256(zip_path)
    zip_sha256_path = write_sha256_file(zip_path, zip_sha256)
    result = {
        "ok": True,
        "repo_root": str(root),
        "out_dir": str(target_dir),
        "package_zip": str(zip_path),
        "package_zip_sha256": zip_sha256,
        "package_zip_sha256_path": str(zip_sha256_path),
        "manifest_path": str(manifest_path),
        "install_guide": str(install_path),
        "package": package_manifest,
        "file_count": len(package_files) + len(package_manifest["generated_files"]),
    }

    install_path.write_text(install_markdown, encoding="utf-8", newline="\n")
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return result


def _validate_repo_root(root: Path) -> None:
    missing = []
    for name in ("pyproject.toml", "README.md"):
        if not (root / name).is_file():
            missing.append(name)
    if not (root / "src" / "voicevault").is_dir():
        missing.append("src/voicevault")
    if missing:
        raise FileNotFoundError(f"Not a VoiceVault repository root. Missing: {', '.join(missing)}")


def _collect_package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in ROOT_FILES:
        path = root / name
        if path.is_file() and _should_include(root, path):
            files.append(path)
    for dirname in INCLUDE_DIRS:
        directory = root / dirname
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and _should_include(root, path):
                files.append(path)
    unique = {path.resolve(): path for path in files}
    return sorted(unique.values(), key=lambda path: _relative_archive_name(root, path).lower())


def _should_include(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if any(part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info") for part in parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    if path.suffix.lower() == ".zip" and "prototype" in parts:
        return False
    return True


def _write_distribution_zip(
    root: Path,
    package_files: list[Path],
    zip_path: Path,
    package_name: str,
    install_markdown: str,
    package_manifest: dict[str, Any],
) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path in package_files:
            archive.write(path, arcname=f"{package_name}/{_relative_archive_name(root, path)}")
        archive.writestr(f"{package_name}/INSTALL.md", install_markdown)
        archive.writestr(
            f"{package_name}/distribution-manifest.json",
            json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n",
        )


def _relative_archive_name(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _install_markdown() -> str:
    return (
        "# VoiceVault CLI Install Guide\n\n"
        "## Requirements\n\n"
        "- Python 3.11 or newer.\n"
        "- A local VoiceVault knowledge base outside this package.\n\n"
        "## Install\n\n"
        "```powershell\n"
        "python -m pip install .\n"
        "python -m voicevault --version\n"
        "```\n\n"
        "## Verify A Knowledge Base\n\n"
        "```powershell\n"
        "python -m voicevault release prepare --kb E:\\knowledge-base\\voicevault --json\n"
        "```\n\n"
        "## Data Boundary\n\n"
        "This package contains code, docs, examples, and release notes only. Keep private knowledge-base content, secrets, cookies, audio samples, and platform caches outside the package.\n"
    )
