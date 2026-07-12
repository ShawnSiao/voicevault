from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from . import __version__
from .distribution import write_distribution_package
from .kb import KnowledgeBase
from .release_prepare import prepare_release


SHIP_MANIFEST_SCHEMA_VERSION = 1
KB_RELEASE_DIGEST_ENTRIES = [
    "readiness.json",
    "manifest.json",
    "release-summary.md",
    "release-plan.md",
    "release-prepare.json",
    "quickstart.json",
    "quickstart.md",
]


def ship_release(
    repo_root: Path,
    kb: KnowledgeBase,
    *,
    out_dir: Path | None = None,
    drain_jobs: bool = True,
    execute_jobs: bool = False,
) -> dict[str, Any]:
    root = repo_root.resolve()
    target_dir = (out_dir or root / "dist").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    prepare = prepare_release(kb, repo_root=root, drain_jobs=drain_jobs, execute_jobs=execute_jobs)
    package = write_distribution_package(root, out_dir=target_dir)
    manifest_path = target_dir / f"voicevault-v{__version__}-ship-manifest.json"
    summary_path = target_dir / f"voicevault-v{__version__}-ship-summary.md"
    verification_report_path = target_dir / f"voicevault-v{__version__}-verification-report.json"
    release_attestation_path = target_dir / f"voicevault-v{__version__}-release-attestation.json"
    release_attestation_sha256_path = release_attestation_path.with_name(f"{release_attestation_path.name}.sha256")
    release_audit_summary_path = target_dir / f"voicevault-v{__version__}-release-audit-summary.md"
    release_audit_summary_check_path = target_dir / f"voicevault-v{__version__}-release-audit-summary-check.json"
    release_artifact_index_path = target_dir / f"voicevault-v{__version__}-artifact-index.json"
    release_artifact_index_sha256_path = release_artifact_index_path.with_name(f"{release_artifact_index_path.name}.sha256")
    manifest = _build_ship_manifest(
        root,
        kb,
        prepare,
        package,
        summary_path,
        verification_report_path,
        release_attestation_path,
        release_attestation_sha256_path,
        release_audit_summary_path,
        release_audit_summary_check_path,
        release_artifact_index_path,
        release_artifact_index_sha256_path,
    )
    result = {
        "ok": bool(prepare["ok"] and package["ok"]),
        "repo_root": str(root),
        "root": str(kb.root),
        "ship_manifest": str(manifest_path),
        "ship_summary": str(summary_path),
        "verification_report": str(verification_report_path),
        "release_attestation": str(release_attestation_path),
        "release_attestation_sha256": str(release_attestation_sha256_path),
        "release_audit_summary": str(release_audit_summary_path),
        "release_audit_summary_check": str(release_audit_summary_check_path),
        "release_artifact_index": str(release_artifact_index_path),
        "release_artifact_index_sha256": str(release_artifact_index_sha256_path),
        "prepare": prepare,
        "quickstart": prepare["quickstart"],
        "package": package,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    summary_path.write_text(_ship_summary_markdown(manifest), encoding="utf-8", newline="\n")
    release_artifact_index_path.write_text(
        json.dumps(_release_artifact_index(manifest, manifest_path), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_sha256_sidecar(release_artifact_index_sha256_path, release_artifact_index_path)
    return result


def _build_ship_manifest(
    repo_root: Path,
    kb: KnowledgeBase,
    prepare: dict[str, Any],
    package: dict[str, Any],
    summary_path: Path,
    verification_report_path: Path,
    release_attestation_path: Path,
    release_attestation_sha256_path: Path,
    release_audit_summary_path: Path,
    release_audit_summary_check_path: Path,
    release_artifact_index_path: Path,
    release_artifact_index_sha256_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": SHIP_MANIFEST_SCHEMA_VERSION,
        "product": {
            "chinese_name": "声迹",
            "english_name": "VoiceVault",
            "repository": "public-voice-archive",
            "version": __version__,
        },
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ok": bool(prepare["ok"] and package["ok"]),
        "repo_root": str(repo_root),
        "knowledge_base": str(kb.root),
        "artifacts": {
            "ship_summary": str(summary_path),
            "verification_report": str(verification_report_path),
            "release_attestation": str(release_attestation_path),
            "release_attestation_sha256_path": str(release_attestation_sha256_path),
            "release_audit_summary": str(release_audit_summary_path),
            "release_audit_summary_check": str(release_audit_summary_check_path),
            "release_artifact_index": str(release_artifact_index_path),
            "release_artifact_index_sha256": str(release_artifact_index_sha256_path),
            "cli_package": {
                "path": package["package_zip"],
                "sha256": package["package_zip_sha256"],
                "sha256_path": package["package_zip_sha256_path"],
                "manifest": package["manifest_path"],
                "install_guide": package["install_guide"],
                "package_entry_sha256": _zip_member_digests(Path(package["package_zip"])),
            },
            "kb_release": {
                "bundle_dir": prepare["bundle"]["bundle_dir"],
                "bundle_zip": prepare["bundle"]["bundle_zip"],
                "bundle_zip_sha256": prepare["bundle"]["bundle_zip_sha256"],
                "bundle_zip_sha256_path": prepare["bundle"]["bundle_zip_sha256_path"],
                "bundle_entry_sha256": _zip_entry_digests(Path(prepare["bundle"]["bundle_zip"])),
                "prepare_report": prepare["prepare_report"],
                "release_summary": prepare["bundle"]["files"]["release_summary"],
                "release_plan": prepare["bundle"]["files"]["release_plan"],
                "quickstart_json": prepare["quickstart"]["guide_json"],
                "quickstart_markdown": prepare["quickstart"]["guide_markdown"],
            },
            "ui": {
                "index_html": prepare["ui"]["index_html"],
                "data_json": prepare["ui"]["data_json"],
            },
        },
        "readiness": prepare["release_check"],
        "data_boundary": [
            "The ship manifest references local artifacts but does not copy private knowledge-base content into the repository.",
            "CLI package contains code, docs, examples, and release notes only.",
            "KB release bundle contains release metadata and handoff files only.",
        ],
    }


def _ship_summary_markdown(manifest: dict[str, Any]) -> str:
    product = manifest["product"]
    status = "通过" if manifest["ok"] else "未通过"
    cli = manifest["artifacts"]["cli_package"]
    kb = manifest["artifacts"]["kb_release"]
    ui = manifest["artifacts"]["ui"]
    verification_report = manifest["artifacts"]["verification_report"]
    release_attestation = manifest["artifacts"]["release_attestation"]
    release_attestation_sha256 = manifest["artifacts"]["release_attestation_sha256_path"]
    release_audit_summary = manifest["artifacts"]["release_audit_summary"]
    release_audit_summary_check = manifest["artifacts"]["release_audit_summary_check"]
    release_artifact_index = manifest["artifacts"]["release_artifact_index"]
    release_artifact_index_sha256 = manifest["artifacts"]["release_artifact_index_sha256"]
    verify_command = (
        f"voicevault release verify --manifest {manifest['repo_root']}\\dist\\"
        f"voicevault-v{product['version']}-ship-manifest.json --json"
    )
    audit_command = (
        f"voicevault release audit --manifest {manifest['repo_root']}\\dist\\"
        f"voicevault-v{product['version']}-ship-manifest.json --summary --summary-out {release_audit_summary}"
    )
    audit_check_command = (
        f"voicevault release audit --manifest {manifest['repo_root']}\\dist\\"
        f"voicevault-v{product['version']}-ship-manifest.json --summary-check {release_audit_summary} "
        f"--summary-check-out {release_audit_summary_check} --json"
    )
    inspect_command = (
        f"voicevault release inspect --manifest {manifest['repo_root']}\\dist\\"
        f"voicevault-v{product['version']}-ship-manifest.json --json"
    )
    return (
        "# VoiceVault Ship Manifest\n\n"
        f"- 产品：{product['chinese_name']} / {product['english_name']}\n"
        f"- 版本：{product['version']}\n"
        f"- 最终验收：{status}\n\n"
        "## CLI 分发包\n\n"
        f"- Zip：{cli['path']}\n"
        f"- SHA256：{cli['sha256']}\n"
        f"- SHA256 file：{cli['sha256_path']}\n"
        f"- Install guide：{cli['install_guide']}\n\n"
        "## KB 发布包\n\n"
        f"- Bundle：{kb['bundle_dir']}\n"
        f"- Zip：{kb['bundle_zip']}\n"
        f"- SHA256：{kb['bundle_zip_sha256']}\n"
        f"- SHA256 file：{kb['bundle_zip_sha256_path']}\n"
        f"- Prepare report：{kb['prepare_report']}\n\n"
        "## Local UI\n\n"
        f"- Index HTML：{ui['index_html']}\n"
        f"- Data JSON：{ui['data_json']}\n\n"
        "## Quickstart Guide\n\n"
        f"- Quickstart JSON：{kb['quickstart_json']}\n"
        f"- Quickstart Markdown：{kb['quickstart_markdown']}\n\n"
        "## Release Artifact Index\n\n"
        f"- Artifact index：{release_artifact_index}\n\n"
        f"- Artifact index SHA256：{release_artifact_index_sha256}\n\n"
        "## Post-Handoff Verification\n\n"
        "```powershell\n"
        f"{verify_command}\n"
        "```\n\n"
        f"- Verification report：{verification_report}\n\n"
        "## Release Audit\n\n"
        "```powershell\n"
        f"{audit_command}\n"
        f"{audit_check_command}\n"
        "```\n\n"
        f"- Audit summary：{release_audit_summary}\n\n"
        f"- Audit summary check：{release_audit_summary_check}\n\n"
        "## Release Inspect\n\n"
        "```powershell\n"
        f"{inspect_command}\n"
        "```\n\n"
        "## Release Attestation\n\n"
        f"- Attestation：{release_attestation}\n"
        f"- Attestation SHA256 file：{release_attestation_sha256}\n\n"
        "Expected checks:\n\n"
        "- `cli_package_sha256`\n"
        "- `ship_manifest_contract`\n"
        "- `verification_report_contract`\n"
        "- `release_attestation_contract`\n"
        "- `release_attestation_archive_contract`\n"
        "- `release_attestation_sidecar_contract`\n"
        "- `release_artifact_index_contract`\n"
        "- `release_artifact_index_sidecar_contract`\n"
        "- `cli_package_sidecar_contract`\n"
        "- `cli_package_manifest_contract`\n"
        "- `cli_install_guide_contract`\n"
        "- `cli_package_distribution_manifest_contract`\n"
        "- `cli_package_entry_digests_contract`\n"
        "- `cli_package_import_smoke`\n"
        "- `cli_package_install_smoke`\n"
        "- `kb_release_sha256`\n"
        "- `kb_release_sidecar_contract`\n"
        "- `kb_prepare_report_contract`\n"
        "- `kb_release_bundle_contract`\n"
        "- `kb_release_entry_digests_contract`\n"
        "- `kb_release_handoff_docs_contract`\n"
        "- `kb_release_quickstart_entries`\n"
        "- `kb_quickstart_guide_contract`\n"
        "- `role_coverage`\n"
        "- `kb_ui_data`\n"
        "- `kb_ui_data_contract`\n"
        "- `kb_answer_export_contracts`\n"
        "- `kb_comparison_export_contracts`\n"
        "- `kb_ui_release_actions`\n\n"
        "## 数据边界\n\n"
        "- 不把真实知识库、私有采集文件、密钥、cookie、音频样本或平台缓存写入仓库。\n"
    )


def _release_artifact_index(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    product = manifest["product"]
    repo_root = manifest["repo_root"]
    knowledge_base = manifest["knowledge_base"]
    version = product["version"]
    artifacts = manifest["artifacts"]
    release_audit_summary = artifacts["release_audit_summary"]
    release_audit_summary_check = artifacts["release_audit_summary_check"]
    return {
        "schema_version": 1,
        "product": product,
        "generated_at": manifest.get("generated_at", ""),
        "ship_manifest": str(manifest_path),
        "commands": {
            "prepare": f"voicevault release prepare --kb {knowledge_base} --root {repo_root} --json",
            "ship": f"voicevault release ship --root {repo_root} --kb {knowledge_base} --json",
            "verify": f"voicevault release verify --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json --json",
            "audit_summary": (
                f"voicevault release audit --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json "
                f"--summary --summary-out {release_audit_summary}"
            ),
            "audit_summary_check": (
                f"voicevault release audit --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json "
                f"--summary-check {release_audit_summary} --summary-check-out {release_audit_summary_check} --json"
            ),
            "inspect": f"voicevault release inspect --manifest {repo_root}\\dist\\voicevault-v{version}-ship-manifest.json --json",
        },
        "artifacts": _release_artifact_entries(manifest, manifest_path),
        "data_boundary": list(manifest.get("data_boundary", [])) if isinstance(manifest.get("data_boundary"), list) else [],
    }


def _release_artifact_entries(manifest: dict[str, Any], manifest_path: Path) -> list[dict[str, Any]]:
    artifacts = manifest["artifacts"]
    cli = artifacts["cli_package"]
    kb = artifacts["kb_release"]
    ui = artifacts["ui"]
    return [
        _artifact_entry("ship_manifest", "json", str(manifest_path), "ship", True, "Final release manifest."),
        _artifact_entry("ship_summary", "markdown", artifacts["ship_summary"], "ship", True, "Human release handoff summary."),
        _artifact_entry("release_artifact_index", "json", artifacts["release_artifact_index"], "ship", True, "Machine-readable release artifact index.", sha256=""),
        _artifact_entry("release_artifact_index_sha256", "sha256", artifacts["release_artifact_index_sha256"], "ship", True, "Release artifact index SHA256 sidecar.", sha256=""),
        _artifact_entry("cli_package", "zip", cli["path"], "ship", True, "Installable CLI package.", sha256=cli["sha256"]),
        _artifact_entry("cli_package_sha256", "sha256", cli["sha256_path"], "ship", True, "CLI package SHA256 sidecar."),
        _artifact_entry("cli_package_manifest", "json", cli["manifest"], "ship", True, "External CLI package manifest."),
        _artifact_entry("cli_install_guide", "markdown", cli["install_guide"], "ship", True, "CLI install guide."),
        _artifact_entry("kb_release_zip", "zip", kb["bundle_zip"], "ship", True, "Knowledge-base release handoff bundle.", sha256=kb["bundle_zip_sha256"]),
        _artifact_entry("kb_release_sha256", "sha256", kb["bundle_zip_sha256_path"], "ship", True, "KB release bundle SHA256 sidecar."),
        _artifact_entry("kb_prepare_report", "json", kb["prepare_report"], "ship", True, "Release prepare orchestration report."),
        _artifact_entry("kb_release_summary", "markdown", kb["release_summary"], "ship", True, "KB release human summary."),
        _artifact_entry("kb_release_plan", "markdown", kb["release_plan"], "ship", True, "KB release execution plan."),
        _artifact_entry("kb_quickstart_json", "json", kb["quickstart_json"], "ship", True, "Quickstart guide JSON."),
        _artifact_entry("kb_quickstart_markdown", "markdown", kb["quickstart_markdown"], "ship", True, "Quickstart guide Markdown."),
        _artifact_entry("ui_index", "html", ui["index_html"], "ship", True, "Local UI HTML."),
        _artifact_entry("ui_data", "json", ui["data_json"], "ship", True, "Local UI data JSON."),
        _artifact_entry("verification_report", "json", artifacts["verification_report"], "post_verify", True, "Release verification report.", sha256=""),
        _artifact_entry("release_attestation", "json", artifacts["release_attestation"], "post_verify", True, "Release attestation.", sha256=""),
        _artifact_entry("release_attestation_sha256", "sha256", artifacts["release_attestation_sha256_path"], "post_verify", True, "Release attestation SHA256 sidecar.", sha256=""),
        _artifact_entry("release_audit_summary", "markdown", artifacts["release_audit_summary"], "post_audit", True, "Read-only release audit summary.", sha256=""),
        _artifact_entry("release_audit_summary_check", "json", artifacts["release_audit_summary_check"], "post_audit", True, "Release audit summary consistency evidence.", sha256=""),
    ]


def _artifact_entry(
    artifact_id: str,
    kind: str,
    path: str,
    phase: str,
    required: bool,
    description: str,
    *,
    sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "kind": kind,
        "path": path,
        "phase": phase,
        "required": required,
        "sha256": sha256 if sha256 is not None else _file_sha256_if_exists(Path(path)),
        "description": description,
    }


def _file_sha256_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_sha256_sidecar(sidecar_path: Path, target_path: Path) -> None:
    digest = _file_sha256_if_exists(target_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(f"{digest}  {target_path.name}\n", encoding="utf-8", newline="\n")


def _zip_entry_digests(zip_path: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    with ZipFile(zip_path) as archive:
        for entry in KB_RELEASE_DIGEST_ENTRIES:
            digests[entry] = hashlib.sha256(archive.read(entry)).hexdigest()
    return digests


def _zip_member_digests(zip_path: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    with ZipFile(zip_path) as archive:
        for name in sorted(item for item in archive.namelist() if not item.endswith("/")):
            digests[name] = hashlib.sha256(archive.read(name)).hexdigest()
    return digests
