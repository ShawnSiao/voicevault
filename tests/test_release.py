from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from voicevault import __version__
from voicevault.answer import answer_query, default_answer_dir, write_answer_outputs
from voicevault.answer_regression import upsert_answer_regression_question
from voicevault.cli import main
from voicevault.comparison import compare_roles, default_comparison_dir, review_comparison_export, write_comparison_outputs
from voicevault.distribution import write_distribution_package
from voicevault.importers import load_statements_from_kb
from voicevault.index import VoiceVaultIndex
from voicevault.kb import init_kb
from voicevault.release import check_release_readiness, write_release_bundle, write_release_manifest
from voicevault.release_prepare import prepare_release
from voicevault.release_ship import ship_release
from voicevault.release_verify import verify_ship_manifest
from voicevault.role_agent import ask_role_agent
from voicevault.role_skill import distill_role_skill, write_role_skill
from voicevault.source_jobs import enqueue_source_jobs, read_source_job_status
from voicevault.sources import create_source


class ReleaseTests(unittest.TestCase):
    def test_project_build_system_declares_wheel_for_distribution_install(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertIn("wheel", pyproject["build-system"]["requires"])

    def test_write_distribution_package_creates_installable_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)

            result = write_distribution_package(repo)

            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["package_zip"]).is_file())
            self.assertTrue(Path(result["manifest_path"]).is_file())
            self.assertTrue(Path(result["install_guide"]).is_file())
            self.assertEqual(result["package_zip_sha256"], _file_sha256(Path(result["package_zip"])))
            self.assertTrue(Path(result["package_zip_sha256_path"]).is_file())
            self.assertEqual(result["package"]["schema_version"], 1)
            self.assertEqual(result["package"]["product"]["english_name"], "VoiceVault")
            with ZipFile(result["package_zip"]) as archive:
                names = archive.namelist()
            prefix = f"voicevault-cli-v{result['package']['product']['version']}/"
            self.assertIn(prefix + "pyproject.toml", names)
            self.assertIn(prefix + "README.md", names)
            self.assertIn(prefix + "src/voicevault/__init__.py", names)
            self.assertIn(prefix + "docs/release/voicevault-v0.16.0.md", names)
            self.assertIn(prefix + "INSTALL.md", names)
            self.assertIn(prefix + "distribution-manifest.json", names)
            self.assertFalse(any(".voicevault/" in name for name in names))
            self.assertFalse(any("__pycache__/" in name for name in names))
            self.assertFalse(any(name.endswith(".pyc") for name in names))
            self.assertFalse(any("/dist/" in name for name in names))
            self.assertFalse(any(name.endswith(".zip") and "prototype/" in name for name in names))

    def test_release_package_json_outputs_distribution_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "package", "--root", str(repo), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(Path(payload["package_zip"]).is_file())
            self.assertEqual(payload["package_zip_sha256"], _file_sha256(Path(payload["package_zip"])))
            self.assertTrue(Path(payload["package_zip_sha256_path"]).is_file())
            self.assertTrue(Path(payload["manifest_path"]).is_file())
            self.assertTrue(Path(payload["install_guide"]).is_file())

    def test_ship_release_writes_final_handoff_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")

            result = ship_release(repo, kb)
            manifest_path = Path(result["ship_manifest"])
            summary_path = Path(result["ship_summary"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_report = str(repo.resolve() / "dist" / f"voicevault-v{__version__}-verification-report.json")
            expected_attestation = str(repo.resolve() / "dist" / f"voicevault-v{__version__}-release-attestation.json")
            expected_attestation_sha256 = str(
                repo.resolve() / "dist" / f"voicevault-v{__version__}-release-attestation.json.sha256"
            )
            expected_audit_summary = str(
                repo.resolve() / "dist" / f"voicevault-v{__version__}-release-audit-summary.md"
            )
            expected_audit_summary_check = str(
                repo.resolve() / "dist" / f"voicevault-v{__version__}-release-audit-summary-check.json"
            )
            expected_artifact_index = str(repo.resolve() / "dist" / f"voicevault-v{__version__}-artifact-index.json")
            expected_artifact_index_sha256 = str(
                repo.resolve() / "dist" / f"voicevault-v{__version__}-artifact-index.json.sha256"
            )

            self.assertFalse(result["ok"])
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(summary_path.is_file())
            self.assertEqual(result["verification_report"], expected_report)
            self.assertEqual(result["release_attestation_sha256"], expected_attestation_sha256)
            self.assertEqual(result.get("release_audit_summary"), expected_audit_summary)
            self.assertEqual(result.get("release_audit_summary_check"), expected_audit_summary_check)
            self.assertEqual(result.get("release_artifact_index"), expected_artifact_index)
            self.assertEqual(result.get("release_artifact_index_sha256"), expected_artifact_index_sha256)
            self.assertTrue(Path(result["package"]["package_zip"]).is_file())
            self.assertTrue(Path(result["prepare"]["bundle"]["bundle_zip"]).is_file())
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["product"]["english_name"], "VoiceVault")
            self.assertEqual(manifest["artifacts"]["ship_summary"], result["ship_summary"])
            self.assertEqual(manifest["artifacts"]["verification_report"], expected_report)
            self.assertEqual(manifest["artifacts"]["release_attestation"], expected_attestation)
            self.assertEqual(manifest["artifacts"]["release_attestation_sha256_path"], expected_attestation_sha256)
            self.assertEqual(manifest["artifacts"].get("release_audit_summary"), expected_audit_summary)
            self.assertEqual(manifest["artifacts"].get("release_audit_summary_check"), expected_audit_summary_check)
            self.assertEqual(manifest["artifacts"].get("release_artifact_index"), expected_artifact_index)
            self.assertEqual(manifest["artifacts"].get("release_artifact_index_sha256"), expected_artifact_index_sha256)
            self.assertEqual(manifest["artifacts"]["cli_package"]["path"], result["package"]["package_zip"])
            self.assertEqual(manifest["artifacts"]["cli_package"]["sha256"], result["package"]["package_zip_sha256"])
            self.assertIn("package_entry_sha256", manifest["artifacts"]["cli_package"])
            self.assertTrue(
                any(name.endswith("README.md") for name in manifest["artifacts"]["cli_package"]["package_entry_sha256"])
            )
            self.assertEqual(manifest["artifacts"]["kb_release"]["bundle_zip"], result["prepare"]["bundle"]["bundle_zip"])
            self.assertEqual(manifest["artifacts"]["kb_release"]["bundle_zip_sha256"], result["prepare"]["bundle"]["bundle_zip_sha256"])
            self.assertEqual(manifest["artifacts"]["kb_release"]["prepare_report"], result["prepare"]["prepare_report"])
            self.assertEqual(manifest["artifacts"]["kb_release"]["quickstart_json"], result["quickstart"]["guide_json"])
            self.assertEqual(manifest["artifacts"]["kb_release"]["quickstart_markdown"], result["quickstart"]["guide_markdown"])
            self.assertEqual(manifest["artifacts"]["ui"]["index_html"], result["prepare"]["ui"]["index_html"])
            self.assertEqual(manifest["artifacts"]["ui"]["data_json"], result["prepare"]["ui"]["data_json"])
            artifact_index = json.loads(Path(expected_artifact_index).read_text(encoding="utf-8"))
            self.assertTrue(Path(expected_artifact_index_sha256).is_file())
            self.assertEqual(artifact_index["schema_version"], 1)
            self.assertEqual(artifact_index["product"]["version"], __version__)
            self.assertEqual(artifact_index["ship_manifest"], str(manifest_path))
            artifact_ids = {artifact["id"] for artifact in artifact_index["artifacts"]}
            self.assertIn("ship_manifest", artifact_ids)
            self.assertIn("ship_summary", artifact_ids)
            self.assertIn("release_artifact_index_sha256", artifact_ids)
            self.assertIn("cli_package", artifact_ids)
            self.assertIn("kb_release_zip", artifact_ids)
            self.assertIn("verification_report", artifact_ids)
            self.assertIn("release_audit_summary_check", artifact_ids)
            self.assertIn("verify", artifact_index["commands"])
            self.assertIn("audit_summary_check", artifact_index["commands"])
            self.assertIn("inspect", artifact_index["commands"])
            summary = summary_path.read_text(encoding="utf-8")
            verify_command = (
                f"voicevault release verify --manifest {repo.resolve()}\\dist\\"
                f"voicevault-v{__version__}-ship-manifest.json --json"
            )
            audit_command = (
                f"voicevault release audit --manifest {repo.resolve()}\\dist\\"
                f"voicevault-v{__version__}-ship-manifest.json --summary --summary-out {expected_audit_summary}"
            )
            audit_check_command = (
                f"voicevault release audit --manifest {repo.resolve()}\\dist\\"
                f"voicevault-v{__version__}-ship-manifest.json --summary-check {expected_audit_summary} "
                f"--summary-check-out {expected_audit_summary_check} --json"
            )
            inspect_command = (
                f"voicevault release inspect --manifest {repo.resolve()}\\dist\\"
                f"voicevault-v{__version__}-ship-manifest.json --json"
            )
            self.assertIn("VoiceVault Ship Manifest", summary)
            self.assertIn("## Local UI", summary)
            self.assertIn(manifest["artifacts"]["ui"]["index_html"], summary)
            self.assertIn(manifest["artifacts"]["ui"]["data_json"], summary)
            self.assertIn("## Quickstart Guide", summary)
            self.assertIn(manifest["artifacts"]["kb_release"]["quickstart_json"], summary)
            self.assertIn(manifest["artifacts"]["kb_release"]["quickstart_markdown"], summary)
            self.assertIn("## Release Artifact Index", summary)
            self.assertIn(expected_artifact_index, summary)
            self.assertIn(expected_artifact_index_sha256, summary)
            self.assertIn("## Post-Handoff Verification", summary)
            self.assertIn(verify_command, summary)
            self.assertIn("Verification report", summary)
            self.assertIn(expected_report, summary)
            self.assertIn("## Release Audit", summary)
            self.assertIn(expected_audit_summary, summary)
            self.assertIn(expected_audit_summary_check, summary)
            self.assertIn(audit_command, summary)
            self.assertIn(audit_check_command, summary)
            self.assertIn("## Release Inspect", summary)
            self.assertIn(inspect_command, summary)
            self.assertIn("## Release Attestation", summary)
            self.assertIn(expected_attestation, summary)
            self.assertIn(expected_attestation_sha256, summary)
            self.assertIn("ship_manifest_contract", summary)
            self.assertIn("verification_report_contract", summary)
            self.assertIn("release_attestation_contract", summary)
            self.assertIn("release_attestation_archive_contract", summary)
            self.assertIn("release_attestation_sidecar_contract", summary)
            self.assertIn("release_artifact_index_contract", summary)
            self.assertIn("release_artifact_index_sidecar_contract", summary)
            self.assertIn("cli_package_sidecar_contract", summary)
            self.assertIn("cli_package_manifest_contract", summary)
            self.assertIn("cli_install_guide_contract", summary)
            self.assertIn("cli_package_distribution_manifest_contract", summary)
            self.assertIn("cli_package_entry_digests_contract", summary)
            self.assertIn("cli_package_install_smoke", summary)
            self.assertIn("kb_release_sidecar_contract", summary)
            self.assertIn("kb_prepare_report_contract", summary)
            self.assertIn("kb_release_bundle_contract", summary)
            self.assertIn("kb_release_entry_digests_contract", summary)
            self.assertIn("kb_release_handoff_docs_contract", summary)
            self.assertIn("kb_ui_data_contract", summary)
            self.assertIn("kb_answer_export_contracts", summary)
            self.assertIn("kb_ui_release_actions", summary)
            self.assertIn("kb_quickstart_guide_contract", summary)
            self.assertIn("kb_release_quickstart_entries", summary)

    def test_ship_release_does_not_record_stale_self_referential_artifact_index_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")

            first = ship_release(repo, kb)
            for path_key in [
                "verification_report",
                "release_attestation",
                "release_attestation_sha256",
                "release_audit_summary",
                "release_audit_summary_check",
            ]:
                Path(first[path_key]).write_text(f"previous {path_key}\n", encoding="utf-8")
            result = ship_release(repo, kb)

            artifact_index = json.loads(Path(result["release_artifact_index"]).read_text(encoding="utf-8"))
            artifact_by_id = {artifact["id"]: artifact for artifact in artifact_index["artifacts"]}
            volatile_artifact_ids = [
                "release_artifact_index",
                "release_artifact_index_sha256",
                "verification_report",
                "release_attestation",
                "release_attestation_sha256",
                "release_audit_summary",
                "release_audit_summary_check",
            ]
            for artifact_id in volatile_artifact_ids:
                with self.subTest(artifact_id=artifact_id):
                    self.assertEqual(artifact_by_id[artifact_id]["sha256"], "")

    def test_release_ship_json_outputs_final_handoff_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "ship", "--root", str(repo), "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertTrue(Path(payload["ship_manifest"]).is_file())
            self.assertTrue(Path(payload["ship_summary"]).is_file())
            self.assertTrue(Path(payload["package"]["package_zip"]).is_file())
            self.assertTrue(Path(payload["prepare"]["bundle"]["bundle_zip"]).is_file())

    def test_verify_ship_manifest_accepts_valid_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest = json.loads(Path(ship["ship_manifest"]).read_text(encoding="utf-8"))

            result = verify_ship_manifest(Path(ship["ship_manifest"]))
            report_path = Path(result["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertTrue(result["ok"])
            self.assertEqual(result["report_path"], manifest["artifacts"]["verification_report"])
            self.assertTrue(report_path.is_file())
            self.assertEqual(report["ok"], result["ok"])
            self.assertEqual(report["manifest_path"], result["manifest_path"])
            self.assertEqual(report["report_path"], result["report_path"])
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(result["product"]["english_name"], "VoiceVault")
            self.assertEqual(result["product"]["version"], __version__)
            self.assertEqual(report["product"], result["product"])
            self.assertEqual(result["summary"]["total"], len(result["checks"]))
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertEqual(result["summary"]["failed_ids"], [])
            self.assertEqual(report["summary"], result["summary"])
            check_ids = {check["id"] for check in result["checks"]}
            self.assertIn("ship_manifest_contract", check_ids)
            self.assertIn("verification_report_contract", check_ids)
            self.assertIn("release_attestation_contract", check_ids)
            self.assertIn("release_attestation_archive_contract", check_ids)
            self.assertIn("release_attestation_sidecar_contract", check_ids)
            self.assertIn("cli_package_sha256", check_ids)
            self.assertIn("cli_package_boundary", check_ids)
            self.assertIn("cli_package_entry_points", check_ids)
            self.assertIn("cli_package_version", check_ids)
            self.assertIn("cli_package_manifest_version", check_ids)
            self.assertIn("cli_package_distribution_manifest_contract", check_ids)
            self.assertIn("cli_package_entry_digests_contract", check_ids)
            self.assertIn("cli_package_sidecar_contract", check_ids)
            self.assertIn("cli_package_manifest_contract", check_ids)
            self.assertIn("cli_install_guide_contract", check_ids)
            self.assertIn("cli_package_import_smoke", check_ids)
            self.assertIn("cli_package_install_smoke", check_ids)
            self.assertIn("kb_release_sha256", check_ids)
            self.assertIn("kb_release_sidecar_contract", check_ids)
            self.assertIn("kb_prepare_report", check_ids)
            self.assertIn("kb_prepare_report_contract", check_ids)
            self.assertIn("kb_release_prepare_entry", check_ids)
            self.assertIn("kb_release_bundle_contract", check_ids)
            self.assertIn("kb_release_entry_digests_contract", check_ids)
            self.assertIn("kb_release_handoff_docs_contract", check_ids)
            self.assertIn("kb_quickstart_guide", check_ids)
            self.assertIn("kb_release_quickstart_entries", check_ids)
            self.assertIn("kb_quickstart_guide_contract", check_ids)
            self.assertIn("kb_ui_data", check_ids)
            self.assertIn("kb_ui_data_contract", check_ids)
            self.assertIn("kb_answer_export_contracts", check_ids)
            self.assertIn("kb_comparison_export_contracts", check_ids)
            self.assertIn("kb_ui_release_actions", check_ids)
            self.assertIn("ship_summary_handoff", check_ids)
            self.assertEqual(result["attestation_path"], manifest["artifacts"]["release_attestation"])
            self.assertEqual(result["attestation_sha256_path"], manifest["artifacts"]["release_attestation_sha256_path"])
            attestation_path = Path(result["attestation_path"])
            attestation_sha256_path = Path(result["attestation_sha256_path"])
            self.assertTrue(attestation_path.is_file())
            self.assertTrue(attestation_sha256_path.is_file())
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            self.assertEqual(attestation["schema_version"], 1)
            self.assertEqual(attestation["status"], "accepted")
            self.assertEqual(attestation["product"]["version"], __version__)
            self.assertEqual(attestation["manifest_path"], result["manifest_path"])
            self.assertEqual(attestation["verification_report"], result["report_path"])
            self.assertEqual(attestation["artifact_sha256"]["cli_package"], manifest["artifacts"]["cli_package"]["sha256"])
            self.assertEqual(attestation["artifact_sha256"]["kb_release"], manifest["artifacts"]["kb_release"]["bundle_zip_sha256"])
            self.assertIn("release_attestation_contract", attestation["required_checks"])
            self.assertIn("release_attestation_archive_contract", attestation["required_checks"])
            self.assertIn("release_attestation_sidecar_contract", attestation["required_checks"])
            self.assertEqual(attestation["summary"]["failed"], 0)
            expected_sidecar = f"{_file_sha256(attestation_path)}  {attestation_path.name}\n"
            self.assertEqual(attestation_sha256_path.read_text(encoding="utf-8"), expected_sidecar)
            attestation_text = attestation_path.read_text(encoding="utf-8")
            sidecar_text = attestation_sha256_path.read_text(encoding="utf-8")

            second = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertTrue(second["ok"])
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), attestation_text)
            self.assertEqual(attestation_sha256_path.read_text(encoding="utf-8"), sidecar_text)

    def test_verify_ship_manifest_rejects_ship_manifest_contract_with_placeholder_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 0
            manifest["repo_root"] = "<repo_root>"
            manifest["data_boundary"] = []
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("ship_manifest_contract", failed)
            self.assertIn("verification_report_contract", {check["id"] for check in result["checks"]})
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["product"]["version"], __version__)
            self.assertEqual(result["summary"]["total"], len(result["checks"]))
            self.assertGreater(result["summary"]["failed"], 0)
            self.assertIn("ship_manifest_contract", result["summary"]["failed_ids"])
            contract = next(check for check in result["checks"] if check["id"] == "ship_manifest_contract")
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])
            self.assertTrue(
                any("repo_root" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("data_boundary" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_ui_data_missing_core_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            manifest = json.loads(Path(ship["ship_manifest"]).read_text(encoding="utf-8"))
            ui_data_path = Path(manifest["artifacts"]["ui"]["data_json"])
            ui_data = json.loads(ui_data_path.read_text(encoding="utf-8"))
            ui_data.pop("analysis_exports")
            ui_data.pop("next_actions")
            ui_data.pop("next_action_audit")
            ui_data.pop("action_runs")
            ui_data.pop("remediation_queue")
            ui_data.pop("answer_quality")
            ui_data.pop("answer_regression")
            ui_data["summary"].pop("next_actions", None)
            ui_data["summary"].pop("completed_next_actions", None)
            ui_data["summary"].pop("action_runs", None)
            ui_data["summary"].pop("action_run_failed", None)
            ui_data["summary"].pop("action_run_retryable_failed", None)
            ui_data["summary"].pop("remediation_items", None)
            ui_data["summary"].pop("remediation_ready", None)
            ui_data["summary"].pop("remediation_blocked", None)
            ui_data["summary"].pop("answer_quality_passed", None)
            ui_data["summary"].pop("answer_quality_review", None)
            ui_data["summary"].pop("answer_quality_failed", None)
            ui_data["summary"].pop("answer_regression_passed", None)
            ui_data["summary"].pop("answer_regression_review", None)
            ui_data["summary"].pop("answer_regression_failed", None)
            ui_data["summary"].pop("answer_regression_questions", None)
            ui_data["summary"].pop("answer_regression_min_questions", None)
            ui_data["summary"].pop("answer_regression_missing_provenance", None)
            ui_data["schema_version"] = 0
            ui_data_path.write_text(json.dumps(ui_data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            contract = next(check for check in result["checks"] if check["id"] == "kb_ui_data_contract")
            self.assertIn("kb_ui_data_contract", failed)
            self.assertIn("analysis_exports", contract["details"]["missing_fields"])
            self.assertIn("next_actions", contract["details"]["missing_fields"])
            self.assertIn("next_action_audit", contract["details"]["missing_fields"])
            self.assertIn("action_runs", contract["details"]["missing_fields"])
            self.assertIn("remediation_queue", contract["details"]["missing_fields"])
            self.assertIn("answer_quality", contract["details"]["missing_fields"])
            self.assertIn("answer_regression", contract["details"]["missing_fields"])
            self.assertIn("summary.next_actions", contract["details"]["missing_fields"])
            self.assertIn("summary.completed_next_actions", contract["details"]["missing_fields"])
            self.assertIn("summary.action_runs", contract["details"]["missing_fields"])
            self.assertIn("summary.action_run_failed", contract["details"]["missing_fields"])
            self.assertIn("summary.action_run_retryable_failed", contract["details"]["missing_fields"])
            self.assertIn("summary.remediation_items", contract["details"]["missing_fields"])
            self.assertIn("summary.remediation_ready", contract["details"]["missing_fields"])
            self.assertIn("summary.remediation_blocked", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_quality_passed", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_quality_review", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_quality_failed", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_passed", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_review", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_failed", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_questions", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_min_questions", contract["details"]["missing_fields"])
            self.assertIn("summary.answer_regression_missing_provenance", contract["details"]["missing_fields"])
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])

    def test_verify_ship_manifest_rejects_legacy_answer_export_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest = json.loads(Path(ship["ship_manifest"]).read_text(encoding="utf-8"))
            ui_data = json.loads(Path(manifest["artifacts"]["ui"]["data_json"]).read_text(encoding="utf-8"))
            answer_path = Path(ui_data["answer_exports"][0]["answer_json"])
            payload = json.loads(answer_path.read_text(encoding="utf-8"))
            payload.pop("schema_version")
            answer_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            contract = next(check for check in result["checks"] if check["id"] == "kb_answer_export_contracts")
            self.assertIn("kb_answer_export_contracts", failed)
            self.assertIn(str(answer_path), contract["details"]["invalid_paths"])
            self.assertIn("schema_version", contract["details"]["invalid_exports"][0]["contract_errors"][0])

    def test_verify_ship_manifest_rejects_unreviewed_comparison_export_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            comparison = compare_roles(kb, "NVDA margin", symbol="NVDA", roles="all", limit=3, evidence_limit=1)
            write_comparison_outputs(default_comparison_dir(kb, "NVDA margin"), comparison)
            ship = ship_release(repo, kb)

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            contract = next(check for check in result["checks"] if check["id"] == "kb_comparison_export_contracts")
            self.assertIn("kb_comparison_export_contracts", failed)
            self.assertEqual(contract["details"]["draft_comparison_exports"], 1)

    def test_verify_ship_manifest_rejects_ui_actions_with_repo_root_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            manifest = json.loads(Path(ship["ship_manifest"]).read_text(encoding="utf-8"))
            ui_data_path = Path(manifest["artifacts"]["ui"]["data_json"])
            ui_data = json.loads(ui_data_path.read_text(encoding="utf-8"))
            ui_data["repo_root"] = "<repo_root>"
            ui_data["release_actions"] = [
                {
                    "status": "ready",
                    "phase": "release_handoff",
                    "check_id": "release_ship",
                    "action": "Run release ship.",
                    "command": f"voicevault release ship --root <repo_root> --kb {kb.root} --json",
                }
            ]
            ui_data_path.write_text(json.dumps(ui_data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("kb_ui_release_actions", failed)

    def test_verify_ship_manifest_rejects_kb_release_bundle_with_corrupt_manifest_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            bundle_zip = Path(ship["prepare"]["bundle"]["bundle_zip"])
            _rewrite_zip_member(
                bundle_zip,
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": 0,
                        "product": {"version": "0.0.0"},
                        "knowledge_base": str(kb.root),
                        "readiness": {"ok": True},
                    },
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
            digest = _file_sha256(bundle_zip)
            Path(ship["prepare"]["bundle"]["bundle_zip_sha256_path"]).write_text(
                f"{digest}  {bundle_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["kb_release"]["bundle_zip_sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            contract = next(check for check in result["checks"] if check["id"] == "kb_release_bundle_contract")
            self.assertIn("kb_release_bundle_contract", failed)
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])
            self.assertTrue(
                any("manifest.product.version" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_kb_release_handoff_docs_contract_with_incomplete_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            bundle_zip = Path(ship["prepare"]["bundle"]["bundle_zip"])
            corrupt_plan = (
                "# 发布上线计划\n\n"
                f"版本：{__version__}\n\n"
                "## 发布前\n\n"
                f"- 运行 `voicevault release check --kb {kb.root} --json`。\n"
                "- 运行 `voicevault release package --root <repo> --json`。\n"
                f"- 运行 `voicevault answers list --kb {kb.root} --status invalid --json`。\n\n"
                "## 发布\n\n"
                "- 归档本目录和同名 zip。\n\n"
                "## 发布后\n\n"
                "- 新增角色后重新运行 release prepare。\n"
            )
            _rewrite_zip_member(bundle_zip, "release-plan.md", corrupt_plan.encode("utf-8"))
            digest = _file_sha256(bundle_zip)
            Path(ship["prepare"]["bundle"]["bundle_zip_sha256_path"]).write_text(
                f"{digest}  {bundle_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["kb_release"]["bundle_zip_sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertNotIn("kb_release_bundle_contract", failed)
            self.assertIn("kb_release_handoff_docs_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_release_handoff_docs_contract")
            self.assertTrue(
                any("release verify" in error or "<repo>" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_kb_release_bundle_with_wrong_manifest_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            bundle_zip = Path(ship["prepare"]["bundle"]["bundle_zip"])
            with ZipFile(bundle_zip) as archive:
                release_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            release_manifest["repo_root"] = "<repo_root>"
            _rewrite_zip_member(
                bundle_zip,
                "manifest.json",
                json.dumps(release_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            digest = _file_sha256(bundle_zip)
            Path(ship["prepare"]["bundle"]["bundle_zip_sha256_path"]).write_text(
                f"{digest}  {bundle_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["kb_release"]["bundle_zip_sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("kb_release_bundle_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_release_bundle_contract")
            self.assertTrue(
                any("manifest.repo_root" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_kb_release_entry_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            bundle_zip = Path(ship["prepare"]["bundle"]["bundle_zip"])
            with ZipFile(bundle_zip) as archive:
                readiness = json.loads(archive.read("readiness.json").decode("utf-8"))
            readiness["checks"][0]["message"] = "Tampered readiness message."
            _rewrite_zip_member(
                bundle_zip,
                "readiness.json",
                json.dumps(readiness, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            digest = _file_sha256(bundle_zip)
            Path(ship["prepare"]["bundle"]["bundle_zip_sha256_path"]).write_text(
                f"{digest}  {bundle_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["kb_release"]["bundle_zip_sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertNotIn("kb_release_sha256", failed)
            self.assertNotIn("kb_release_sha256_sidecar_match", failed)
            self.assertNotIn("kb_release_bundle_contract", failed)
            self.assertIn("kb_release_entry_digests_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_release_entry_digests_contract")
            self.assertTrue(
                any("readiness.json" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_quickstart_guide_contract_with_placeholder_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            quickstart_json = Path(manifest["artifacts"]["kb_release"]["quickstart_json"])
            quickstart = json.loads(quickstart_json.read_text(encoding="utf-8"))
            quickstart["schema_version"] = 0
            quickstart["repo_root"] = "<repo_root>"
            quickstart["phases"][-1]["commands"] = [
                f"voicevault release verify --manifest <repo_root>\\dist\\voicevault-v{__version__}-ship-manifest.json --json"
            ]
            quickstart_json.write_text(json.dumps(quickstart, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("kb_quickstart_guide_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_quickstart_guide_contract")
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])
            self.assertTrue(
                any("repo_root" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_prepare_report_contract_with_missing_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            prepare_report = Path(manifest["artifacts"]["kb_release"]["prepare_report"])
            report = json.loads(prepare_report.read_text(encoding="utf-8"))
            report["schema_version"] = 0
            report["repo_root"] = "<repo_root>"
            report["steps"] = [step for step in report["steps"] if step["id"] != "release_check"]
            prepare_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("kb_prepare_report_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_prepare_report_contract")
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])
            self.assertTrue(
                any("repo_root" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("release_check" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_corrupted_ship_summary_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            Path(ship["ship_summary"]).write_text("# Broken handoff\n", encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))
            report_path = Path(result["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertFalse(result["ok"])
            self.assertTrue(report_path.is_file())
            self.assertFalse(report["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            failed_in_report = {check["id"] for check in report["checks"] if not check["ok"]}
            self.assertIn("ship_summary_handoff", failed)
            self.assertIn("ship_summary_handoff", failed_in_report)

    def test_verify_ship_manifest_rejects_tampered_release_artifact_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            index_path = Path(ship["release_artifact_index"])
            artifact_index = json.loads(index_path.read_text(encoding="utf-8"))
            artifact_index["commands"].pop("audit_summary_check")
            index_path.write_text(json.dumps(artifact_index, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("release_artifact_index_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "release_artifact_index_contract")
            self.assertTrue(
                any("commands.audit_summary_check" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_tampered_release_artifact_index_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            sidecar_path = Path(ship["release_artifact_index_sha256"])
            sidecar_path.write_text("0" * 64 + "  voicevault-vbad-artifact-index.json\n", encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("release_artifact_index_sidecar_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "release_artifact_index_sidecar_contract")
            self.assertTrue(
                any("digest" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("filename" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_release_attestation_contract_with_placeholder_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["release_attestation"] = "<repo_root>\\dist\\voicevault-release-attestation.json"
            manifest["artifacts"]["release_attestation_sha256_path"] = (
                "<repo_root>\\dist\\voicevault-release-attestation.json.sha256"
            )
            manifest["artifacts"]["release_audit_summary"] = "<repo_root>\\dist\\voicevault-release-audit-summary.md"
            manifest["artifacts"]["release_audit_summary_check"] = (
                "<repo_root>\\dist\\voicevault-release-audit-summary-check.json"
            )
            manifest["artifacts"]["release_artifact_index"] = "<repo_root>\\dist\\voicevault-artifact-index.json"
            manifest["artifacts"]["release_artifact_index_sha256"] = (
                "<repo_root>\\dist\\voicevault-artifact-index.json.sha256"
            )
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("ship_manifest_contract", failed)
            self.assertIn("release_attestation_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "release_attestation_contract")
            self.assertTrue(
                any("release_attestation" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )
            ship_contract = next(check for check in result["checks"] if check["id"] == "ship_manifest_contract")
            self.assertTrue(
                any("release_attestation_sha256_path" in error for error in ship_contract["details"]["contract_errors"]),
                ship_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("release_audit_summary" in error for error in ship_contract["details"]["contract_errors"]),
                ship_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("release_audit_summary_check" in error for error in ship_contract["details"]["contract_errors"]),
                ship_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("release_artifact_index" in error for error in ship_contract["details"]["contract_errors"]),
                ship_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("release_artifact_index_sha256" in error for error in ship_contract["details"]["contract_errors"]),
                ship_contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_tampered_existing_release_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            first = verify_ship_manifest(Path(ship["ship_manifest"]))
            attestation_path = Path(first["attestation_path"])
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            attestation["status"] = "rejected"
            attestation_path.write_text(json.dumps(attestation, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            second = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(second["ok"])
            failed = {check["id"] for check in second["checks"] if not check["ok"]}
            self.assertIn("release_attestation_archive_contract", failed)
            preserved = json.loads(attestation_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["status"], "rejected")

    def test_verify_ship_manifest_rejects_tampered_release_attestation_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)

            first = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(first["ok"])
            attestation_path = Path(first["attestation_path"])
            sidecar_path = Path(first["attestation_sha256_path"])
            original_attestation = attestation_path.read_text(encoding="utf-8")
            bad_sidecar = "0" * 64 + "  wrong-file.json\n"
            sidecar_path.write_text(bad_sidecar, encoding="utf-8", newline="\n")

            second = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(second["ok"])
            failed = {check["id"] for check in second["checks"] if not check["ok"]}
            self.assertIn("release_attestation_sidecar_contract", failed)
            self.assertNotIn("release_attestation_archive_contract", failed)
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), bad_sidecar)
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), original_attestation)

    def test_release_verify_json_outputs_validation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "verify", "--manifest", ship["ship_manifest"], "--json"])

            payload = json.loads(stdout.getvalue())
            report_path = Path(payload["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(report_path.is_file())
            self.assertEqual(payload["manifest_path"], ship["ship_manifest"])
            self.assertEqual(report["ok"], payload["ok"])
            self.assertEqual(report["manifest_path"], payload["manifest_path"])
            self.assertEqual(report["report_path"], payload["report_path"])

    def test_release_audit_json_is_read_only_and_requires_archived_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                first_exit = main(["release", "audit", "--manifest", ship["ship_manifest"], "--json"])

            first_payload = json.loads(stdout.getvalue())
            failed = {check["id"] for check in first_payload["checks"] if not check["ok"]}
            self.assertEqual(first_exit, 1)
            self.assertFalse(first_payload["ok"])
            self.assertFalse(first_payload["write_artifacts"])
            self.assertIn("release_attestation_archive_contract", failed)
            self.assertIn("release_attestation_sidecar_contract", failed)
            self.assertFalse(Path(first_payload["report_path"]).exists())
            self.assertFalse(Path(first_payload["attestation_path"]).exists())
            self.assertFalse(Path(first_payload["attestation_sha256_path"]).exists())

            verify = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(verify["ok"])
            report_path = Path(verify["report_path"])
            attestation_path = Path(verify["attestation_path"])
            sidecar_path = Path(verify["attestation_sha256_path"])
            report_text = report_path.read_text(encoding="utf-8")
            attestation_text = attestation_path.read_text(encoding="utf-8")
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                second_exit = main(["release", "audit", "--manifest", ship["ship_manifest"], "--json"])

            second_payload = json.loads(stdout.getvalue())
            self.assertEqual(second_exit, 0)
            self.assertTrue(second_payload["ok"])
            self.assertFalse(second_payload["write_artifacts"])
            self.assertEqual(report_path.read_text(encoding="utf-8"), report_text)
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), attestation_text)
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), sidecar_text)

    def test_release_audit_summary_outputs_read_only_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            verify = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(verify["ok"])
            report_path = Path(verify["report_path"])
            attestation_path = Path(verify["attestation_path"])
            sidecar_path = Path(verify["attestation_sha256_path"])
            report_text = report_path.read_text(encoding="utf-8")
            attestation_text = attestation_path.read_text(encoding="utf-8")
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "audit", "--manifest", ship["ship_manifest"], "--summary"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("# VoiceVault Release Audit", output)
            self.assertIn(f"- Version: {__version__}", output)
            self.assertIn("- Status: ok", output)
            self.assertIn("- Write artifacts: false", output)
            self.assertIn("- Checks: 45 passed / 0 failed", output)
            self.assertIn("## Artifacts", output)
            self.assertIn(verify["attestation_path"], output)
            self.assertIn(verify["attestation_sha256_path"], output)
            self.assertNotIn("{", output)
            self.assertEqual(report_path.read_text(encoding="utf-8"), report_text)
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), attestation_text)
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), sidecar_text)

    def test_release_audit_summary_lists_blocking_checks_and_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "audit", "--manifest", ship["ship_manifest"], "--summary"])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 1)
            self.assertIn("- Status: needs attention", output)
            self.assertIn("- Write artifacts: false", output)
            self.assertIn("## Blocking Checks", output)
            self.assertIn("### release_attestation_archive_contract", output)
            self.assertIn("### release_attestation_sidecar_contract", output)
            self.assertIn("- Message: Existing release attestation contract is incomplete.", output)
            self.assertIn("- Message: Release attestation SHA256 sidecar contract is incomplete.", output)
            self.assertIn("- existing: false", output)
            self.assertIn("- existing release attestation is required for read-only audit", output)
            self.assertIn("- release attestation sidecar is required for read-only audit", output)
            self.assertIn(
                f"python -m voicevault release verify --manifest {ship['ship_manifest']} --json",
                output,
            )
            self.assertIn(
                f"python -m voicevault release audit --manifest {ship['ship_manifest']} --summary",
                output,
            )
            self.assertFalse(Path(ship["verification_report"]).exists())
            self.assertFalse(Path(ship["release_attestation"]).exists())
            self.assertFalse(Path(ship["release_attestation_sha256"]).exists())

    def test_release_audit_summary_out_writes_markdown_without_release_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            summary_path = Path(temp_dir) / "audit" / "release-audit-summary.md"
            stdout = io.StringIO()
            stderr = io.StringIO()

            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "release",
                            "audit",
                            "--manifest",
                            ship["ship_manifest"],
                            "--summary",
                            "--summary-out",
                            str(summary_path),
                        ]
                    )
            except SystemExit as exc:
                exit_code = int(exc.code)

            self.assertTrue(summary_path.is_file())
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), summary_text)
            self.assertIn("# VoiceVault Release Audit", summary_text)
            self.assertIn("## Blocking Checks", summary_text)
            self.assertIn("release_attestation_archive_contract", summary_text)
            self.assertTrue(summary_text.endswith("\n"))
            self.assertFalse(Path(ship["verification_report"]).exists())
            self.assertFalse(Path(ship["release_attestation"]).exists())
            self.assertFalse(Path(ship["release_attestation_sha256"]).exists())

    def test_release_audit_summary_check_verifies_archived_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            verify = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(verify["ok"])
            report_path = Path(verify["report_path"])
            attestation_path = Path(verify["attestation_path"])
            sidecar_path = Path(verify["attestation_sha256_path"])
            report_text = report_path.read_text(encoding="utf-8")
            attestation_text = attestation_path.read_text(encoding="utf-8")
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            summary_path = Path(ship["release_audit_summary"])

            with contextlib.redirect_stdout(io.StringIO()):
                write_exit = main(
                    [
                        "release",
                        "audit",
                        "--manifest",
                        ship["ship_manifest"],
                        "--summary-out",
                        str(summary_path),
                    ]
                )
            self.assertEqual(write_exit, 0)
            stdout = io.StringIO()

            try:
                with contextlib.redirect_stdout(stdout):
                    check_exit = main(
                        [
                            "release",
                            "audit",
                            "--manifest",
                            ship["ship_manifest"],
                            "--summary-check",
                            str(summary_path),
                            "--json",
                        ]
                    )
            except SystemExit as exc:
                check_exit = int(exc.code)

            payload = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
            self.assertEqual(check_exit, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["summary_check"]["ok"])
            self.assertTrue(payload["summary_check"]["exists"])
            self.assertEqual(payload["summary_check"]["path"], str(summary_path))
            self.assertRegex(payload["summary_check"]["expected_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(payload["summary_check"]["expected_sha256"], payload["summary_check"]["actual_sha256"])

            summary_path.write_text(summary_path.read_text(encoding="utf-8") + "\nTampered\n", encoding="utf-8", newline="\n")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                tampered_exit = main(
                    [
                        "release",
                        "audit",
                        "--manifest",
                        ship["ship_manifest"],
                        "--summary-check",
                        str(summary_path),
                        "--json",
                    ]
                )
            tampered_payload = json.loads(stdout.getvalue())
            self.assertEqual(tampered_exit, 1)
            self.assertFalse(tampered_payload["ok"])
            self.assertFalse(tampered_payload["summary_check"]["ok"])
            self.assertEqual(
                tampered_payload["summary_check"]["message"],
                "Archived audit summary does not match the current audit summary.",
            )
            self.assertNotEqual(
                tampered_payload["summary_check"]["expected_sha256"],
                tampered_payload["summary_check"]["actual_sha256"],
            )
            self.assertEqual(report_path.read_text(encoding="utf-8"), report_text)
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), attestation_text)
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), sidecar_text)

    def test_release_audit_summary_check_out_writes_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            verify = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(verify["ok"])
            report_path = Path(verify["report_path"])
            attestation_path = Path(verify["attestation_path"])
            sidecar_path = Path(verify["attestation_sha256_path"])
            report_text = report_path.read_text(encoding="utf-8")
            attestation_text = attestation_path.read_text(encoding="utf-8")
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            summary_path = Path(ship["release_audit_summary"])
            evidence_path = Path(ship["release_audit_summary_check"])

            with contextlib.redirect_stdout(io.StringIO()):
                write_exit = main(
                    [
                        "release",
                        "audit",
                        "--manifest",
                        ship["ship_manifest"],
                        "--summary-out",
                        str(summary_path),
                    ]
                )
            self.assertEqual(write_exit, 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                check_exit = main(
                    [
                        "release",
                        "audit",
                        "--manifest",
                        ship["ship_manifest"],
                        "--summary-check",
                        str(summary_path),
                        "--summary-check-out",
                        str(evidence_path),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(check_exit, 0)
            self.assertTrue(evidence_path.is_file())
            self.assertEqual(payload["summary_check_report"], str(evidence_path))
            self.assertEqual(evidence["schema_version"], 1)
            self.assertTrue(evidence["ok"])
            self.assertFalse(evidence["write_artifacts"])
            self.assertEqual(evidence["manifest_path"], ship["ship_manifest"])
            self.assertEqual(evidence["summary_check"]["path"], str(summary_path))
            self.assertTrue(evidence["summary_check"]["ok"])
            self.assertEqual(
                evidence["summary_check"]["expected_sha256"],
                evidence["summary_check"]["actual_sha256"],
            )
            self.assertEqual(report_path.read_text(encoding="utf-8"), report_text)
            self.assertEqual(attestation_path.read_text(encoding="utf-8"), attestation_text)
            self.assertEqual(sidecar_path.read_text(encoding="utf-8"), sidecar_text)

    def test_release_inspect_reports_complete_handoff_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            verify = verify_ship_manifest(Path(ship["ship_manifest"]))
            self.assertTrue(verify["ok"])
            summary_path = Path(ship["release_audit_summary"])
            evidence_path = Path(ship["release_audit_summary_check"])

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "release",
                            "audit",
                            "--manifest",
                            ship["ship_manifest"],
                            "--summary-out",
                            str(summary_path),
                        ]
                    ),
                    0,
                )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "release",
                            "audit",
                            "--manifest",
                            ship["ship_manifest"],
                            "--summary-check",
                            str(summary_path),
                            "--summary-check-out",
                            str(evidence_path),
                            "--json",
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "inspect", "--manifest", ship["ship_manifest"], "--json"])

            payload = json.loads(stdout.getvalue())
            artifact_by_id = {artifact["id"]: artifact for artifact in payload["artifacts"]}
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["product"]["version"], __version__)
            self.assertEqual(payload["summary"]["missing_required"], 0)
            self.assertEqual(payload["summary"]["sha256_mismatched"], 0)
            self.assertIn("verify", payload["commands"])
            self.assertIn("audit_summary_check", payload["commands"])
            self.assertIn("inspect", payload["commands"])
            self.assertTrue(artifact_by_id["verification_report"]["exists"])
            self.assertTrue(artifact_by_id["release_audit_summary_check"]["exists"])

    def test_release_inspect_reports_missing_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            self.assertFalse(Path(ship["verification_report"]).exists())
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "inspect", "--manifest", ship["ship_manifest"], "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("verification_report", payload["summary"]["missing_required_ids"])
            self.assertFalse(Path(ship["verification_report"]).exists())

    def test_release_verify_json_writes_custom_validation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            custom_report = Path(temp_dir) / "reports" / "verify.json"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "release",
                        "verify",
                        "--manifest",
                        ship["ship_manifest"],
                        "--out",
                        str(custom_report),
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            report = json.loads(custom_report.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["report_path"], str(custom_report.resolve()))
            self.assertEqual(report["report_path"], str(custom_report.resolve()))
            self.assertTrue(report["ok"])

    def test_verify_ship_manifest_rejects_tampered_cli_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            Path(ship["package"]["package_zip"]).write_bytes(b"tampered")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("cli_package_sha256", failed)

    def test_verify_ship_manifest_rejects_cli_package_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            _rewrite_zip_member(
                package_zip,
                "src/voicevault/__init__.py",
                b'__version__ = "0.0.0"\n',
            )
            digest = _file_sha256(package_zip)
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{digest}  {package_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["cli_package"]["sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("cli_package_version", failed)

    def test_verify_ship_manifest_rejects_cli_distribution_manifest_contract_with_missing_generated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            package_manifest = dict(ship["package"]["package"])
            package_manifest["schema_version"] = 0
            package_manifest["generated_files"] = ["INSTALL.md"]
            package_manifest["data_boundary"] = []
            _rewrite_zip_member(
                package_zip,
                "distribution-manifest.json",
                json.dumps(package_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            digest = _file_sha256(package_zip)
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{digest}  {package_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["cli_package"]["sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("cli_package_distribution_manifest_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "cli_package_distribution_manifest_contract")
            self.assertIn("schema_version", contract["details"]["contract_errors"][0])
            self.assertTrue(
                any("generated_files" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("data_boundary" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_cli_package_entry_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            _rewrite_zip_member(package_zip, "README.md", b"# Tampered VoiceVault\n")
            digest = _file_sha256(package_zip)
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{digest}  {package_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            external_manifest_path = Path(ship["package"]["manifest_path"])
            external_manifest = json.loads(external_manifest_path.read_text(encoding="utf-8"))
            external_manifest["package_zip_sha256"] = digest
            external_manifest_path.write_text(
                json.dumps(external_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["cli_package"]["sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertNotIn("cli_package_sha256", failed)
            self.assertNotIn("cli_package_sha256_sidecar_match", failed)
            self.assertNotIn("cli_package_manifest_contract", failed)
            self.assertNotIn("cli_package_distribution_manifest_contract", failed)
            self.assertIn("cli_package_entry_digests_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "cli_package_entry_digests_contract")
            self.assertTrue(
                any("README.md" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_cli_external_handoff_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{ship['package']['package_zip_sha256']}  wrong-name.zip\n",
                encoding="utf-8",
                newline="\n",
            )
            external_manifest_path = Path(ship["package"]["manifest_path"])
            external_manifest = json.loads(external_manifest_path.read_text(encoding="utf-8"))
            external_manifest["package"]["schema_version"] = 0
            external_manifest["package_zip_sha256"] = "bad-digest"
            external_manifest["file_count"] = 0
            external_manifest_path.write_text(
                json.dumps(external_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            Path(ship["package"]["install_guide"]).write_text("# Broken\n", encoding="utf-8", newline="\n")

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertNotIn("cli_package_sha256", failed)
            self.assertNotIn("cli_package_sha256_sidecar_match", failed)
            self.assertIn("cli_package_sidecar_contract", failed)
            self.assertIn("cli_package_manifest_contract", failed)
            self.assertIn("cli_install_guide_contract", failed)
            sidecar_contract = next(check for check in result["checks"] if check["id"] == "cli_package_sidecar_contract")
            manifest_contract = next(check for check in result["checks"] if check["id"] == "cli_package_manifest_contract")
            guide_contract = next(check for check in result["checks"] if check["id"] == "cli_install_guide_contract")
            self.assertTrue(
                any("filename" in error for error in sidecar_contract["details"]["contract_errors"]),
                sidecar_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("schema_version" in error for error in manifest_contract["details"]["contract_errors"]),
                manifest_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("package_zip_sha256" in error for error in manifest_contract["details"]["contract_errors"]),
                manifest_contract["details"]["contract_errors"],
            )
            self.assertTrue(
                any("missing required fragment" in error for error in guide_contract["details"]["contract_errors"]),
                guide_contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_kb_release_sidecar_contract_with_wrong_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            answer = answer_query(kb, "NVDA margin", symbol="NVDA", limit=2)
            write_answer_outputs(default_answer_dir(kb, "NVDA margin"), answer)
            ship = ship_release(repo, kb)
            bundle = ship["prepare"]["bundle"]
            Path(bundle["bundle_zip_sha256_path"]).write_text(
                f"{bundle['bundle_zip_sha256']}  wrong-kb-release.zip\n",
                encoding="utf-8",
                newline="\n",
            )

            result = verify_ship_manifest(Path(ship["ship_manifest"]))

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertNotIn("kb_release_sha256", failed)
            self.assertNotIn("kb_release_sha256_sidecar_match", failed)
            self.assertIn("kb_release_sidecar_contract", failed)
            contract = next(check for check in result["checks"] if check["id"] == "kb_release_sidecar_contract")
            self.assertTrue(
                any("filename" in error for error in contract["details"]["contract_errors"]),
                contract["details"]["contract_errors"],
            )

    def test_verify_ship_manifest_rejects_cli_package_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            _rewrite_zip_member(
                package_zip,
                "src/voicevault/cli.py",
                b"def main(:\n    return 0\n",
            )
            digest = _file_sha256(package_zip)
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{digest}  {package_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["cli_package"]["sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("cli_package_import_smoke", failed)

    def test_verify_ship_manifest_rejects_cli_package_install_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            _write_minimal_distribution_repo(repo)
            kb = init_kb(Path(temp_dir) / "voicevault")
            ship = ship_release(repo, kb)
            package_zip = Path(ship["package"]["package_zip"])
            _rewrite_zip_member(
                package_zip,
                "pyproject.toml",
                b"[project\nname = \"voicevault\"\n",
            )
            digest = _file_sha256(package_zip)
            Path(ship["package"]["package_zip_sha256_path"]).write_text(
                f"{digest}  {package_zip.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest_path = Path(ship["ship_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["cli_package"]["sha256"] = digest
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

            result = verify_ship_manifest(manifest_path)

            self.assertFalse(result["ok"])
            failed = {check["id"] for check in result["checks"] if not check["ok"]}
            self.assertIn("cli_package_install_smoke", failed)

    def test_check_release_readiness_reports_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            generated_role = kb.roles_dir / "draft-role"
            generated_role.mkdir()
            (generated_role / "profile.generated.md").write_text(
                "---\nrole_id: draft-role\nprofile_status: generated_unreviewed\n---\n",
                encoding="utf-8",
            )

            report = check_release_readiness(kb)

            self.assertFalse(report["ok"])
            failed = {check["id"] for check in report["checks"] if not check["ok"]}
            self.assertIn("index", failed)
            self.assertIn("sync_status", failed)
            self.assertIn("capture_status", failed)
            self.assertIn("sources", failed)
            self.assertIn("source_runs", failed)
            self.assertIn("source_adapters", failed)
            self.assertIn("answer_exports", failed)
            self.assertIn("answer_regression", failed)
            self.assertIn("reports", failed)
            self.assertIn("dashboard", failed)
            self.assertIn("ui", failed)
            self.assertIn("profiles_reviewed", failed)
            self.assertIn("sample_content", failed)
            profile_check = next(check for check in report["checks"] if check["id"] == "profiles_reviewed")
            self.assertIn("voicevault profile generate", profile_check["remediation"])
            capture_check = next(check for check in report["checks"] if check["id"] == "capture_status")
            self.assertIn("voicevault sync", capture_check["remediation"])
            source_check = next(check for check in report["checks"] if check["id"] == "sources")
            self.assertIn("voicevault sources create", source_check["remediation"])
            source_runs_check = next(check for check in report["checks"] if check["id"] == "source_runs")
            self.assertIn("voicevault sources run", source_runs_check["remediation"])
            source_adapters_check = next(check for check in report["checks"] if check["id"] == "source_adapters")
            self.assertIn("voicevault sources validate", source_adapters_check["remediation"])
            regression_check = next(check for check in report["checks"] if check["id"] == "answer_regression")
            self.assertIn("voicevault evaluations answers", regression_check["remediation"])
            self.assertEqual(report["summary"]["answer_regression_questions"], 0)
            self.assertEqual(report["summary"]["answer_regression_min_questions"], 4)
            sample_check = next(check for check in report["checks"] if check["id"] == "sample_content")
            self.assertIn("voicevault sample remove", sample_check["remediation"])

    def test_release_check_json_passes_after_full_local_artifacts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            capture_path = kb.inbox_captures_dir / "growth.jsonl"
            capture_path.write_text(
                json.dumps(
                    {
                        "role_id": "growth-analyst",
                        "platform": "x",
                        "author": "Growth Analyst",
                        "url": "https://x.com/growth/status/1",
                        "published_at": "2026-05-30T12:00:00Z",
                        "captured_at": "2026-05-31T01:00:00Z",
                        "title": "NVDA demand",
                        "text": "NVDA demand remains durable after earnings.",
                        "symbols": ["NVDA"],
                        "topics": ["earnings"],
                        "stance": "bullish",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with capture_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        {
                            "role_id": "value-analyst",
                            "platform": "x",
                            "author": "Value Analyst",
                            "url": "https://x.com/value/status/1",
                            "published_at": "2026-05-30T13:00:00Z",
                            "captured_at": "2026-05-31T01:10:00Z",
                            "title": "NVDA valuation",
                            "text": "NVDA demand is strong, but valuation still needs margin discipline.",
                            "symbols": ["NVDA"],
                            "topics": ["earnings"],
                            "stance": "mixed",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            source_input = Path(temp_dir) / "growth-source.jsonl"
            source_input.write_text(
                '{"text":"Release import validates configured source health.","source_url":"https://x.com/growth/status/2","symbols":["NVDA"],"topics":["earnings"]}\n',
                encoding="utf-8",
            )

            commands = [
                ["sync", "--kb", str(kb.root), "--archive"],
                [
                    "sources",
                    "create",
                    "--kb",
                    str(kb.root),
                    "--source",
                    "x-growth-analyst",
                    "--role",
                    "growth-analyst",
                    "--platform",
                    "x",
                    "--source-url",
                    "https://x.com/growth",
                ],
                [
                    "sources",
                    "import",
                    "--kb",
                    str(kb.root),
                    "--source",
                    "x-growth-analyst",
                    "--input",
                    str(source_input),
                ],
                ["profile", "generate", "--role", "growth-analyst", "--kb", str(kb.root)],
                ["profile", "promote", "--role", "growth-analyst", "--kb", str(kb.root)],
                ["profile", "generate", "--role", "value-analyst", "--kb", str(kb.root)],
                ["profile", "promote", "--role", "value-analyst", "--kb", str(kb.root)],
                [
                    "event",
                    "create",
                    "--kb",
                    str(kb.root),
                    "--event-id",
                    "2026-05-31-nvda-demand",
                    "--title",
                    "NVIDIA Demand",
                    "--date",
                    "2026-05-31",
                    "--symbols",
                    "NVDA",
                    "--topics",
                    "earnings",
                    "--summary",
                    "Investors debate demand durability after earnings.",
                ],
                [
                    "analyze",
                    "--kb",
                    str(kb.root),
                    "--event",
                    str(kb.events_dir / "2026-05-31-nvda-demand.md"),
                    "--roles",
                    "growth-analyst",
                ],
                ["sample", "remove", "--kb", str(kb.root)],
                [
                    "collect",
                    "--kb",
                    str(kb.root),
                    "--title",
                    "NVDA Demand Evidence",
                    "--query",
                    "NVDA demand",
                    "--symbol",
                    "NVDA",
                ],
                ["dashboard", "--kb", str(kb.root)],
                ["answer", "--kb", str(kb.root), "--query", "NVDA demand", "--role", "growth-analyst", "--symbol", "NVDA"],
                ["answer", "--kb", str(kb.root), "--query", "NVDA earnings", "--role", "growth-analyst", "--symbol", "NVDA"],
                ["answer", "--kb", str(kb.root), "--query", "NVDA valuation", "--role", "value-analyst", "--symbol", "NVDA"],
                ["answer", "--kb", str(kb.root), "--query", "NVDA margin", "--role", "value-analyst", "--symbol", "NVDA"],
                ["role", "distill", "--kb", str(kb.root), "--role", "growth-analyst"],
                ["role", "distill", "--kb", str(kb.root), "--role", "value-analyst"],
                [
                    "role",
                    "ask",
                    "--kb",
                    str(kb.root),
                    "--role",
                    "growth-analyst",
                    "--query",
                    "How would the role reason about NVDA demand?",
                    "--symbol",
                    "NVDA",
                    "--dry-run",
                ],
                [
                    "role",
                    "ask",
                    "--kb",
                    str(kb.root),
                    "--role",
                    "value-analyst",
                    "--query",
                    "How would the role reason about NVDA valuation?",
                    "--symbol",
                    "NVDA",
                    "--dry-run",
                ],
                ["compare", "--kb", str(kb.root), "--query", "NVDA demand", "--symbol", "NVDA", "--roles", "all"],
                [
                    "comparisons",
                    "review",
                    "--kb",
                    str(kb.root),
                    "--query",
                    "NVDA demand",
                    "--status",
                    "adopted",
                    "--reviewer",
                    "codex-product-review",
                    "--notes",
                    "Approved for release readiness.",
                ],
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                for command in commands:
                    self.assertEqual(main(command), 0)
                _write_release_regression_suite(kb)
                self.assertEqual(main(["ui", "--kb", str(kb.root), "--root", str(repo)]), 0)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "check", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(all(check["ok"] for check in payload["checks"]))
            self.assertIn("capture_status", {check["id"] for check in payload["checks"]})
            self.assertIn("sources", {check["id"] for check in payload["checks"]})
            self.assertIn("source_runs", {check["id"] for check in payload["checks"]})
            self.assertIn("source_adapters", {check["id"] for check in payload["checks"]})
            self.assertIn("source_jobs", {check["id"] for check in payload["checks"]})
            self.assertIn("answer_exports", {check["id"] for check in payload["checks"]})
            self.assertIn("answer_regression", {check["id"] for check in payload["checks"]})
            self.assertIn("role_skills", {check["id"] for check in payload["checks"]})
            self.assertIn("role_agent_quality", {check["id"] for check in payload["checks"]})
            self.assertIn("role_agent_readiness", {check["id"] for check in payload["checks"]})
            self.assertIn("comparison_exports", {check["id"] for check in payload["checks"]})
            self.assertIn("ui", {check["id"] for check in payload["checks"]})
            self.assertEqual(payload["summary"]["source_runs"], 1)
            self.assertEqual(payload["summary"]["source_adapter_failed"], 0)
            self.assertEqual(payload["summary"]["source_imports"], 1)
            self.assertEqual(payload["summary"]["source_import_failed"], 0)
            self.assertEqual(payload["summary"]["source_jobs_failed"], 0)
            self.assertEqual(payload["summary"]["analysis_export_ready"], 1)
            self.assertEqual(payload["summary"]["analysis_export_malformed"], 0)
            self.assertEqual(payload["summary"]["answer_regression_questions"], 4)
            self.assertEqual(payload["summary"]["answer_regression_missing_provenance"], 0)
            self.assertEqual(payload["summary"]["role_skills_missing"], 0)
            self.assertEqual(payload["summary"]["role_skills_ready"], 2)
            self.assertEqual(payload["summary"]["role_agent_exports"], 2)
            self.assertEqual(payload["summary"]["role_agent_failed"], 0)
            self.assertEqual(payload["summary"]["role_agent_roles_prompt_ready"], 2)
            self.assertEqual(payload["summary"]["role_agent_roles_live_ready"], 0)
            self.assertEqual(payload["summary"]["role_agent_roles_missing_live"], 2)
            self.assertEqual(payload["summary"]["comparison_exports"], 1)
            self.assertEqual(payload["summary"]["adopted_comparison_exports"], 1)
            analysis_check = next(check for check in payload["checks"] if check["id"] == "analysis_exports")
            self.assertTrue(analysis_check["ok"])
            self.assertEqual(analysis_check["details"]["summary"]["ready"], 1)
            self.assertEqual(analysis_check["details"]["summary"]["malformed"], 0)
            ui_data = json.loads((kb.exports_dir / "ui" / "data.json").read_text(encoding="utf-8"))
            self.assertTrue(ui_data["summary"]["release_ready"])
            self.assertEqual(ui_data["summary"]["source_imports"], 1)
            self.assertEqual(ui_data["repo_root"], str(repo.resolve()))
            self.assertNotIn("<repo_root>", ui_data["release_actions"][0]["command"])
            self.assertIn(str(repo.resolve()), ui_data["release_actions"][0]["command"])

    def test_release_check_exposes_role_agent_live_readiness_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))
            ask_role_agent(
                kb,
                "sample-investor",
                "How would this role think about NVDA margins?",
                symbol="NVDA",
                dry_run=True,
            )

            advisory_report = check_release_readiness(kb)
            live_report = check_release_readiness(kb, require_live_role_agent=True)
            advisory_check = next(check for check in advisory_report["checks"] if check["id"] == "role_agent_readiness")
            live_check = next(check for check in live_report["checks"] if check["id"] == "role_agent_readiness")

            self.assertTrue(advisory_check["ok"])
            self.assertFalse(advisory_check["details"]["live_ok"])
            self.assertEqual(advisory_report["summary"]["role_agent_roles_prompt_ready"], 1)
            self.assertEqual(advisory_report["summary"]["role_agent_roles_live_ready"], 0)
            self.assertEqual(advisory_report["summary"]["role_agent_roles_missing_live"], 1)
            self.assertFalse(live_check["ok"])
            self.assertFalse(live_report["ok"])
            self.assertEqual(live_check["details"]["summary"]["roles_blocked_runtime"], 1)
            self.assertIn("--call-llm", live_check["remediation"])

    def test_release_check_requires_reviewed_comparison_when_comparisons_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            comparison = compare_roles(kb, "NVDA margin", symbol="NVDA", roles="all", limit=3, evidence_limit=1)
            output = write_comparison_outputs(default_comparison_dir(kb, "NVDA margin"), comparison)

            draft_report = check_release_readiness(kb)
            draft_check = next(check for check in draft_report["checks"] if check["id"] == "comparison_exports")
            review_comparison_export(
                output["comparison_json"],
                status="adopted",
                reviewer="codex-product-review",
                notes="Approved for release readiness.",
            )
            adopted_report = check_release_readiness(kb)
            adopted_check = next(check for check in adopted_report["checks"] if check["id"] == "comparison_exports")

            self.assertFalse(draft_check["ok"])
            self.assertEqual(draft_check["details"]["draft_comparison_exports"], 1)
            self.assertEqual(draft_check["details"]["adopted_comparison_exports"], 0)
            self.assertTrue(adopted_check["ok"])
            self.assertEqual(adopted_check["details"]["adopted_comparison_exports"], 1)

    def test_release_check_requires_multi_role_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            one_role_report = check_release_readiness(kb)
            one_role_check = next(check for check in one_role_report["checks"] if check["id"] == "role_coverage")

            _add_reviewed_growth_role(kb)
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))
            two_role_report = check_release_readiness(kb)
            two_role_check = next(check for check in two_role_report["checks"] if check["id"] == "role_coverage")

            self.assertFalse(one_role_check["ok"])
            self.assertEqual(one_role_report["summary"]["reviewed_roles_with_statements"], 1)
            self.assertEqual(one_role_check["details"]["min_reviewed_roles"], 2)
            self.assertIn("roles coverage", one_role_check["remediation"])
            self.assertTrue(two_role_check["ok"])
            self.assertEqual(two_role_report["summary"]["reviewed_roles_with_statements"], 2)

    def test_release_check_requires_role_skill_coverage_for_agent_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            VoiceVaultIndex(kb).rebuild(load_statements_from_kb(kb))

            missing_report = check_release_readiness(kb)
            missing_check = next(check for check in missing_report["checks"] if check["id"] == "role_skills")
            write_role_skill(kb, distill_role_skill(kb, "sample-investor"))
            ready_report = check_release_readiness(kb)
            ready_check = next(check for check in ready_report["checks"] if check["id"] == "role_skills")

            self.assertFalse(missing_check["ok"])
            self.assertEqual(missing_report["summary"]["role_skills_missing"], 1)
            self.assertIn("voicevault role distill", missing_check["remediation"])
            self.assertTrue(ready_check["ok"])
            self.assertEqual(ready_report["summary"]["role_skills_ready"], 1)
            self.assertIn("role_skills", ready_report["summary"])

    def test_release_check_requires_evidence_backed_answer_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            answer_dir = kb.exports_dir / "answers" / "empty"
            answer_dir.mkdir(parents=True)
            (answer_dir / "answer.json").write_text(
                json.dumps(
                    {
                        "query": "NVDA margin",
                        "generated_at": "2026-05-31T01:00:00Z",
                        "confidence": "low",
                        "coverage": {"evidence_count": 0, "total_matches": 0, "role_count": 0},
                        "citations": [],
                        "answer": "No indexed evidence was found.",
                    }
                ),
                encoding="utf-8",
            )

            report = check_release_readiness(kb)

            answer_check = next(check for check in report["checks"] if check["id"] == "answer_exports")
            self.assertFalse(answer_check["ok"])
            self.assertEqual(answer_check["details"]["total_exports"], 1)
            self.assertEqual(answer_check["details"]["evidence_backed_exports"], 0)
            self.assertEqual(answer_check["details"]["deliverable_answer_exports"], 0)
            self.assertIn(str(answer_dir / "answer.json"), answer_check["details"]["invalid_paths"])

    def test_release_check_source_jobs_remediation_mentions_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )
            enqueue_stdout = io.StringIO()
            with contextlib.redirect_stdout(enqueue_stdout):
                self.assertEqual(main(["sources", "enqueue", "--kb", str(kb.root), "--json"]), 0)
            job_id = json.loads(enqueue_stdout.getvalue())["jobs"][0]["job_id"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sources", "run", "--kb", str(kb.root), "--job", job_id]), 1)

            report = check_release_readiness(kb)

            source_jobs_check = next(check for check in report["checks"] if check["id"] == "source_jobs")
            self.assertFalse(source_jobs_check["ok"])
            self.assertIn("voicevault sources retry", source_jobs_check["remediation"])

    def test_release_check_source_jobs_requires_no_pending_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "x-public-analyst",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                        ]
                    ),
                    0,
                )
                self.assertEqual(main(["sources", "enqueue", "--kb", str(kb.root), "--json"]), 0)

            report = check_release_readiness(kb)

            source_jobs_check = next(check for check in report["checks"] if check["id"] == "source_jobs")
            self.assertFalse(source_jobs_check["ok"])
            self.assertEqual(source_jobs_check["details"]["summary"]["pending"], 1)
            self.assertIn("voicevault sources drain", source_jobs_check["remediation"])

    def test_release_check_requires_chinese_answer_with_key_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            answer_dir = kb.exports_dir / "answers" / "legacy"
            answer_dir.mkdir(parents=True)
            (answer_dir / "answer.json").write_text(
                json.dumps(
                    {
                        "query": "AI",
                        "generated_at": "2026-05-31T01:00:00Z",
                        "confidence": "high",
                        "coverage": {"evidence_count": 2, "total_matches": 2, "role_count": 1},
                        "citations": [{"ref": "[1]"}, {"ref": "[2]"}],
                        "answer": "VoiceVault found 2 cited evidence items.",
                    }
                ),
                encoding="utf-8",
            )

            report = check_release_readiness(kb)

            answer_check = next(check for check in report["checks"] if check["id"] == "answer_exports")
            self.assertFalse(answer_check["ok"])
            self.assertEqual(answer_check["details"]["evidence_backed_exports"], 1)
            self.assertEqual(answer_check["details"]["deliverable_answer_exports"], 0)
            self.assertIn(str(answer_dir / "answer.json"), answer_check["details"]["invalid_paths"])

    def test_release_check_requires_v1_answer_export_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            answer_dir = kb.exports_dir / "answers" / "legacy-v1"
            answer_dir.mkdir(parents=True)
            (answer_dir / "answer.json").write_text(
                json.dumps(
                    {
                        "answer_type": "local_evidence_answer",
                        "answer_language": "zh-CN",
                        "query": "AI",
                        "filters": {"role_id": "", "symbol": "", "topic": "", "limit": 2},
                        "generated_at": "2026-05-31T01:00:00Z",
                        "confidence": "high",
                        "coverage": {"evidence_count": 1, "total_matches": 1, "role_count": 1},
                        "answer": "声迹在本地索引中找到 1 条可引用证据。",
                        "answer_markdown": "# 证据答案\n",
                        "key_points": [{"text": "结构完整但缺少 schema_version。", "refs": ["[1]"], "published_at": "2026-05-31T00:00:00Z"}],
                        "citations": [
                            {
                                "ref": "[1]",
                                "statement_id": "s1",
                                "role_id": "r1",
                                "title": "AI",
                                "source_url": "https://example.com/1",
                                "published_at": "2026-05-31T00:00:00Z",
                                "source_platform": "x",
                            }
                        ],
                        "evidence": [
                            {
                                "ref": "[1]",
                                "statement_id": "s1",
                                "role_id": "r1",
                                "title": "AI",
                                "source_url": "https://example.com/1",
                                "published_at": "2026-05-31T00:00:00Z",
                                "captured_at": "2026-05-31T00:00:00Z",
                                "excerpt": "AI evidence excerpt.",
                            }
                        ],
                        "uncertainty": ["本地规则生成。"],
                        "search": {"query": "AI", "total_matches": 1, "results": []},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
                newline="\n",
            )

            report = check_release_readiness(kb)

            answer_check = next(check for check in report["checks"] if check["id"] == "answer_exports")
            self.assertFalse(answer_check["ok"])
            self.assertEqual(answer_check["details"]["evidence_backed_exports"], 1)
            self.assertEqual(answer_check["details"]["deliverable_answer_exports"], 0)
            self.assertIn(str(answer_dir / "answer.json"), answer_check["details"]["invalid_paths"])
            self.assertIn("schema_version", answer_check["details"]["invalid_exports"][0]["contract_errors"][0])

    def test_release_check_blocks_malformed_analysis_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            bad_dir = kb.exports_dir / "bad-analysis"
            bad_dir.mkdir(parents=True)
            (bad_dir / "analysis.json").write_text("{not json", encoding="utf-8")

            report = check_release_readiness(kb)

            analysis_check = next(check for check in report["checks"] if check["id"] == "analysis_exports")
            self.assertFalse(analysis_check["ok"])
            self.assertEqual(report["summary"]["analysis_exports"], 1)
            self.assertEqual(report["summary"]["analysis_export_ready"], 0)
            self.assertEqual(report["summary"]["analysis_export_malformed"], 1)
            self.assertEqual(analysis_check["details"]["summary"]["malformed"], 1)
            self.assertEqual(analysis_check["details"]["malformed"][0]["analysis_json"], str(bad_dir / "analysis.json"))
            self.assertIn("voicevault analyses list", analysis_check["remediation"])

    def test_release_check_blocks_analysis_exports_with_contract_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            bad_dir = kb.exports_dir / "bad-contract"
            bad_dir.mkdir(parents=True)
            (bad_dir / "analysis.json").write_text(
                json.dumps({"event": {}, "role_analyses": [], "evidence": []}),
                encoding="utf-8",
                newline="\n",
            )

            report = check_release_readiness(kb)

            analysis_check = next(check for check in report["checks"] if check["id"] == "analysis_exports")
            self.assertFalse(analysis_check["ok"])
            self.assertEqual(report["summary"]["analysis_exports"], 1)
            self.assertEqual(report["summary"]["analysis_export_ready"], 0)
            self.assertEqual(report["summary"]["analysis_export_malformed"], 1)
            malformed = analysis_check["details"]["malformed"][0]
            self.assertEqual(malformed["analysis_json"], str(bad_dir / "analysis.json"))
            self.assertIn("event.event_id", malformed["error"])
            self.assertIn("role_analyses", malformed["error"])

    def test_write_release_manifest_records_readiness_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", "--kb", str(kb.root)]), 0)
                self.assertEqual(main(["profile", "generate", "--role", "sample-investor", "--kb", str(kb.root)]), 0)
                self.assertEqual(
                    main(
                        [
                            "collect",
                            "--kb",
                            str(kb.root),
                            "--title",
                            "NVDA Margin Evidence",
                            "--query",
                            "NVDA margin",
                            "--symbol",
                            "NVDA",
                        ]
                    ),
                    0,
                )
                self.assertEqual(main(["sync", "--kb", str(kb.root)]), 0)
                self.assertEqual(main(["dashboard", "--kb", str(kb.root)]), 0)

            manifest_path = write_release_manifest(kb)

            self.assertEqual(manifest_path, kb.exports_dir / "release" / "manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["readiness"]["schema_version"], 1)
            self.assertEqual(manifest["product"]["english_name"], "VoiceVault")
            self.assertEqual(manifest["product"]["version"], __version__)
            self.assertFalse(manifest["readiness"]["ok"])
            self.assertEqual(
                Path(manifest["artifacts"]["dashboard"]).parts[-2:],
                ("dashboard", "index.html"),
            )
            self.assertEqual(
                Path(manifest["artifacts"]["ui"]).parts[-2:],
                ("ui", "index.html"),
            )
            self.assertIn("answer_exports", manifest["artifacts"])
            self.assertIn("source_configs", manifest["artifacts"])
            self.assertIn("source_status", manifest["artifacts"])
            self.assertIn("source_adapter_validation", manifest["artifacts"])
            self.assertIn("source_jobs", manifest["artifacts"])
            self.assertIn("source_import_status", manifest["artifacts"])
            self.assertIn("source_imports", manifest["readiness"]["summary"])
            self.assertIn("analysis_export_status", manifest["artifacts"])
            self.assertIn("analysis_export_malformed", manifest["readiness"]["summary"])

    def test_release_manifest_json_outputs_written_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "manifest", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertTrue(Path(payload["manifest_path"]).is_file())
            self.assertFalse(payload["ok"])

    def test_write_release_bundle_creates_handoff_files_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            bundle = write_release_bundle(kb)

            self.assertFalse(bundle["ok"])
            self.assertTrue(Path(bundle["bundle_dir"]).is_dir())
            self.assertTrue(Path(bundle["bundle_zip"]).is_file())
            self.assertEqual(bundle["bundle_zip_sha256"], _file_sha256(Path(bundle["bundle_zip"])))
            self.assertTrue(Path(bundle["bundle_zip_sha256_path"]).is_file())
            self.assertTrue(Path(bundle["files"]["readiness_json"]).is_file())
            self.assertTrue(Path(bundle["files"]["manifest_json"]).is_file())
            summary = Path(bundle["files"]["release_summary"]).read_text(encoding="utf-8")
            plan = Path(bundle["files"]["release_plan"]).read_text(encoding="utf-8")
            manifest = json.loads(Path(bundle["files"]["manifest_json"]).read_text(encoding="utf-8"))
            readiness = json.loads(Path(bundle["files"]["readiness_json"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(readiness["schema_version"], 1)
            self.assertEqual(manifest["product"]["version"], __version__)
            self.assertEqual(readiness["ok"], bundle["ok"])
            self.assertIn("# 声迹 VoiceVault 发布交付包", summary)
            self.assertIn("发布上线计划", plan)
            self.assertIn("sources import", plan)
            self.assertIn("sources imports", plan)
            self.assertIn("sources normalize", plan)
            self.assertIn("sources drain", plan)
            self.assertIn("Source import status", summary)
            self.assertIn("Analysis exports", summary)
            self.assertIn("Source run status", plan)
            self.assertIn("Source job queue", plan)
            self.assertIn("analyses list", plan)
            self.assertIn("release prepare", plan)
            self.assertIn("release package", plan)
            self.assertIn("release ship", plan)
            self.assertIn("release verify", plan)
            self.assertIn(f"voicevault-v{__version__}-ship-manifest.json", plan)
            self.assertIn(f"voicevault-cli-v{__version__}.zip", plan)
            self.assertNotIn("<repo>", plan)
            self.assertNotIn("<version>", plan)

    def test_prepare_release_drains_pending_jobs_and_writes_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            repo = Path(temp_dir) / "repo"
            repo.mkdir()
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Prepared queued record.","source_url":"https://x.com/public/status/6"}\n',
                encoding="utf-8",
            )
            create_source(
                kb,
                source_id="local-jsonl-source",
                role_id="public-analyst",
                platform="x",
                adapter="local-jsonl",
                adapter_config={"input_path": str(input_path)},
            )
            enqueue_source_jobs(kb, source_id="local-jsonl-source")

            result = prepare_release(kb, repo_root=repo)
            status = read_source_job_status(kb)
            capture_path = kb.inbox_captures_dir / "source-local-jsonl-source.jsonl"
            step_ids = {step["id"] for step in result["steps"]}
            ui_data = json.loads(Path(result["ui"]["data_json"]).read_text(encoding="utf-8"))

            self.assertFalse(result["ok"])
            self.assertEqual(ui_data["repo_root"], str(repo.resolve()))
            self.assertEqual(result["source_jobs_before"]["summary"]["pending"], 1)
            self.assertFalse(result["source_job_drain"]["skipped"])
            self.assertTrue(result["source_job_drain"]["dry_run"])
            self.assertEqual(result["source_job_drain"]["completed"], 1)
            self.assertEqual(result["source_job_drain"]["failed"], 0)
            self.assertEqual(status["summary"]["pending"], 0)
            self.assertFalse(capture_path.exists())
            self.assertTrue(Path(result["dashboard"]).is_file())
            self.assertTrue(Path(result["ui"]["index_html"]).is_file())
            self.assertTrue(Path(result["ui"]["data_json"]).is_file())
            self.assertTrue(Path(result["quickstart"]["guide_json"]).is_file())
            self.assertTrue(Path(result["quickstart"]["guide_markdown"]).is_file())
            self.assertTrue(Path(result["bundle"]["bundle_zip"]).is_file())
            self.assertEqual(result["bundle"]["bundle_zip_sha256"], _file_sha256(Path(result["bundle"]["bundle_zip"])))
            self.assertTrue(Path(result["bundle"]["files"]["release_summary"]).is_file())
            self.assertIn("source_adapters", step_ids)
            self.assertIn("source_jobs", step_ids)
            self.assertIn("dashboard", step_ids)
            self.assertIn("ui", step_ids)
            self.assertIn("quickstart", step_ids)
            self.assertIn("release_bundle", step_ids)
            self.assertIn("release_check", step_ids)

    def test_prepare_release_writes_prepare_report_into_bundle_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            result = prepare_release(kb)
            report_path = Path(result["prepare_report"])
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertTrue(report_path.is_file())
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(result["bundle"]["files"]["release_prepare"], str(report_path))
            self.assertEqual(result["bundle"]["files"]["quickstart_json"], result["quickstart"]["guide_json"])
            self.assertEqual(result["bundle"]["files"]["quickstart_markdown"], result["quickstart"]["guide_markdown"])
            self.assertEqual(report["root"], str(kb.root))
            self.assertEqual(report["prepare_report"], str(report_path))
            self.assertEqual(report["quickstart"]["guide_json"], result["quickstart"]["guide_json"])
            self.assertEqual(result["bundle"]["bundle_zip_sha256"], _file_sha256(Path(result["bundle"]["bundle_zip"])))
            checksum_path = Path(result["bundle"]["bundle_zip_sha256_path"])
            self.assertTrue(checksum_path.is_file())
            self.assertIn(result["bundle"]["bundle_zip_sha256"], checksum_path.read_text(encoding="utf-8"))
            with ZipFile(result["bundle"]["bundle_zip"]) as archive:
                self.assertIn("release-prepare.json", archive.namelist())
                self.assertEqual(archive.namelist().count("release-prepare.json"), 1)
                self.assertEqual(archive.namelist().count("quickstart.json"), 1)
                self.assertEqual(archive.namelist().count("quickstart.md"), 1)

    def test_prepare_release_is_idempotent_for_prepare_report_zip_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")

            prepare_release(kb)
            result = prepare_release(kb)

            with ZipFile(result["bundle"]["bundle_zip"]) as archive:
                self.assertEqual(archive.namelist().count("release-prepare.json"), 1)
                self.assertEqual(archive.namelist().count("quickstart.json"), 1)
                self.assertEqual(archive.namelist().count("quickstart.md"), 1)

    def test_release_bundle_json_outputs_handoff_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "bundle", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertTrue(Path(payload["bundle_zip"]).is_file())
            self.assertTrue(Path(payload["files"]["release_summary"]).is_file())

    def test_release_prepare_json_runs_safe_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kb = init_kb(Path(temp_dir) / "voicevault")
            input_path = Path(temp_dir) / "public-feed.jsonl"
            input_path.write_text(
                '{"text":"Prepared CLI queued record.","source_url":"https://x.com/public/status/7"}\n',
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "sources",
                            "create",
                            "--kb",
                            str(kb.root),
                            "--source",
                            "local-jsonl-source",
                            "--role",
                            "public-analyst",
                            "--platform",
                            "x",
                            "--adapter",
                            "local-jsonl",
                            "--adapter-config",
                            json.dumps({"input_path": str(input_path)}),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(["sources", "enqueue", "--kb", str(kb.root), "--source", "local-jsonl-source"]),
                    0,
                )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(["release", "prepare", "--kb", str(kb.root), "--json"])

            payload = json.loads(stdout.getvalue())
            capture_path = kb.inbox_captures_dir / "source-local-jsonl-source.jsonl"
            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["source_jobs_before"]["summary"]["pending"], 1)
            self.assertTrue(payload["source_job_drain"]["dry_run"])
            self.assertEqual(payload["source_job_drain"]["completed"], 1)
            self.assertEqual(payload["source_job_drain"]["summary"]["pending"], 0)
            self.assertFalse(capture_path.exists())
            self.assertTrue(Path(payload["ui"]["index_html"]).is_file())
            self.assertTrue(Path(payload["quickstart"]["guide_json"]).is_file())
            self.assertTrue(Path(payload["bundle"]["bundle_zip"]).is_file())


def _write_release_regression_suite(kb) -> None:
    questions = [
        ("nvda-demand-growth", "NVDA demand", "growth-analyst", "https://example.com/regression/nvda-demand"),
        ("nvda-earnings-growth", "NVDA earnings", "growth-analyst", "https://example.com/regression/nvda-earnings"),
        ("nvda-valuation-value", "NVDA valuation", "value-analyst", "https://example.com/regression/nvda-valuation"),
        ("nvda-margin-value", "NVDA margin", "value-analyst", "https://example.com/regression/nvda-margin"),
    ]
    for question_id, query, role_id, source_url in questions:
        upsert_answer_regression_question(
            kb,
            {
                "id": question_id,
                "query": query,
                "role_id": role_id,
                "symbol": "NVDA",
                "topic": "earnings",
                "expected_role_id": role_id,
                "source_url": source_url,
                "rationale": f"Protects release readiness coverage for {query}.",
                "updated_by": "codex-release-test",
                "min_evidence": 1,
                "requires_role_answer": True,
            },
        )


def _write_minimal_distribution_repo(repo: Path) -> None:
    (repo / "src" / "voicevault" / "__pycache__").mkdir(parents=True)
    (repo / "docs" / "release").mkdir(parents=True)
    (repo / "docs" / "integration").mkdir(parents=True)
    (repo / "docs" / "product").mkdir(parents=True)
    (repo / "examples").mkdir(parents=True)
    (repo / ".voicevault").mkdir(parents=True)
    (repo / "dist").mkdir(parents=True)
    (repo / "prototype").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires = [\"setuptools>=68\", \"wheel\"]\n"
        "build-backend = \"setuptools.build_meta\"\n\n"
        "[project]\n"
        "name = \"voicevault\"\n"
        f"version = \"{__version__}\"\n"
        "requires-python = \">=3.11\"\n\n"
        "[tool.setuptools.package-dir]\n"
        "\"\" = \"src\"\n\n"
        "[tool.setuptools.packages.find]\n"
        "where = [\"src\"]\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# VoiceVault\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agent Instructions\n", encoding="utf-8")
    (repo / "src" / "voicevault" / "__init__.py").write_text(f"__version__ = \"{__version__}\"\n", encoding="utf-8")
    (repo / "src" / "voicevault" / "__main__.py").write_text("from .cli import main\n\nraise SystemExit(main())\n", encoding="utf-8")
    (repo / "src" / "voicevault" / "cli.py").write_text(
        "from . import __version__\n"
        "import sys\n\n"
        "def main():\n"
        "    if '--version' in sys.argv:\n"
        "        print(__version__)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (repo / "src" / "voicevault" / "__pycache__" / "cli.pyc").write_bytes(b"private-cache")
    (repo / "docs" / "release" / "voicevault-v0.16.0.md").write_text("# Release\n", encoding="utf-8")
    (repo / "docs" / "release" / f"voicevault-v{__version__}.md").write_text("# Current Release\n", encoding="utf-8")
    (repo / "docs" / "integration" / "obsidian.md").write_text("# Integration\n", encoding="utf-8")
    (repo / "docs" / "product" / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    (repo / "examples" / "run.ps1").write_text("python -m voicevault --version\n", encoding="utf-8")
    (repo / ".voicevault" / "secret.json").write_text("{\"token\":\"nope\"}\n", encoding="utf-8")
    (repo / "dist" / "old.zip").write_bytes(b"old")
    (repo / "prototype" / "voicevault-uiux-prototype.zip").write_bytes(b"prototype")


def _add_reviewed_growth_role(kb) -> None:
    role_dir = kb.roles_dir / "growth-investor"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "profile.md").write_text(
        "---\n"
        "role_id: growth-investor\n"
        "display_name: Growth Investor\n"
        "profile_status: reviewed\n"
        "---\n"
        "\n"
        "# Growth Investor\n",
        encoding="utf-8",
        newline="\n",
    )
    (role_dir / "statements.csv").write_text(
        "statement_id,role_id,source_type,source_url,published_at,captured_at,title,body,symbols,topics,stance,time_horizon,confidence,notes\n"
        'growth-1,growth-investor,post,https://example.com/growth,2026-05-30,2026-05-31,Growth view,"NVDA demand remains durable because AI infrastructure spend is broadening.",NVDA,ai-infrastructure,bullish,long_term,medium,\n',
        encoding="utf-8",
        newline="\n",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rewrite_zip_member(zip_path: Path, suffix: str, content: bytes) -> None:
    temp_zip = zip_path.with_name(f"{zip_path.stem}.tmp.zip")
    replaced = False
    with ZipFile(zip_path) as source, ZipFile(temp_zip, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = content if info.filename.endswith(suffix) else source.read(info.filename)
            if info.filename.endswith(suffix):
                replaced = True
            target.writestr(info, payload)
    if not replaced:
        temp_zip.unlink(missing_ok=True)
        raise AssertionError(f"Zip member ending with {suffix} not found.")
    temp_zip.replace(zip_path)


if __name__ == "__main__":
    unittest.main()
