from __future__ import annotations

import json
import ipaddress
import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .api_router import ApiRouter, InvalidJsonError
from .app_db import AppDatabase
from .answer_provider import OpenAICompatibleAnswerProvider, ProviderUnavailable
from .collection_jobs import CollectionService
from .embedding import EmbeddingUnavailable, OpenAICompatibleEmbeddingProvider
from .fulltext_index import LocalFullTextIndexProvider
from .index_jobs import IndexJobService
from .index_service import IndexService
from .retrieval import RetrievalRepository
from .retrieval_service import RetrievalService
from .question_service import QuestionService
from .questions import QuestionRepository
from .runtime import RuntimeRecord, RuntimeRegistry
from .vector_index import LocalVectorIndexProvider

from . import __version__
from .action_runs import record_action_run, read_action_run, resolve_action_run
from .accounts import collect_account, create_account, read_account_status
from .answer import answer_query, default_answer_dir, write_answer_outputs
from .answer_regression import (
    audit_answer_regression,
    delete_answer_regression_question,
    export_answer_regression_suite,
    import_answer_regression_suite,
    load_answer_regression_changelog,
    upsert_answer_regression_question,
)
from .comparison import compare_roles, default_comparison_dir, review_comparison_export, write_comparison_outputs
from .kb import KnowledgeBase
from .onboarding import create_public_role_source, ingest_public_statement
from .role_agent import ask_role_agent
from .role_skill import distill_role_skill, write_role_skill
from .routing import suggest_roles
from .ui import write_local_ui


MAX_JSON_BYTES = 1024 * 1024


class _ThreadedRetrievalExecutor:
    def __init__(self, service: RetrievalService) -> None:
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicevault-retrieval")

    def submit(self, run_id: str) -> None:
        self.pool.submit(self._execute, run_id)

    def _execute(self, run_id: str) -> None:
        try:
            self.service.execute(run_id)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)


class _ThreadedQuestionExecutor:
    def __init__(self, service: QuestionService) -> None:
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicevault-answer")

    def submit(self, run_id: str) -> None:
        self.pool.submit(self._execute, run_id)

    def _execute(self, run_id: str) -> None:
        try:
            self.service.execute(run_id)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)


class _ThreadedIndexExecutor:
    def __init__(self, service: IndexJobService) -> None:
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voicevault-index")

    def submit(self, job_id: str) -> None:
        self.pool.submit(self._execute, job_id)

    def _execute(self, job_id: str) -> None:
        try:
            self.service.run(job_id)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)


class VoiceVaultServer(ThreadingHTTPServer):
    kb: KnowledgeBase
    repo_root: Path | None
    ui_index: Path
    ui_data: Path
    api_router: ApiRouter | None
    collection_service: CollectionService | None
    instance_id: str
    runtime_registry: RuntimeRegistry | None
    retrieval_executor: Any
    question_executor: Any
    index_executor: Any
    _runtime_cleared: bool
    _retrieval_executor_closed: bool
    _question_executor_closed: bool
    _index_executor_closed: bool
    resource_ui_assets: dict[str, tuple[Path, str]]

    def server_close(self) -> None:
        if not self._retrieval_executor_closed:
            shutdown = getattr(self.retrieval_executor, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self._retrieval_executor_closed = True
        if not self._question_executor_closed:
            shutdown = getattr(self.question_executor, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self._question_executor_closed = True
        if not self._index_executor_closed:
            shutdown = getattr(self.index_executor, "shutdown", None)
            if callable(shutdown):
                shutdown()
            self._index_executor_closed = True
        if not self._runtime_cleared and self.runtime_registry is not None:
            self.runtime_registry.clear(self.instance_id)
            self._runtime_cleared = True
        super().server_close()


def create_server(
    kb: KnowledgeBase,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    repo_root: Path | None = None,
    app_database: AppDatabase | None = None,
    collection_service: Any = None,
    retrieval_service: RetrievalService | None = None,
    retrieval_executor: Any = None,
    question_service: QuestionService | None = None,
    question_executor: Any = None,
    index_job_service: IndexJobService | None = None,
    index_executor: Any = None,
    instance_id: str | None = None,
    runtime_registry: Any = None,
) -> VoiceVaultServer:
    if app_database is not None and not _is_loopback_host(host):
        raise ValueError("VoiceVault resource API requires an explicit loopback host.")
    if app_database is not None:
        app_database.initialize()
    elif (
        retrieval_service is not None
        or retrieval_executor is not None
        or question_service is not None
        or question_executor is not None
        or index_job_service is not None
        or index_executor is not None
    ):
        raise ValueError("Retrieval and question resources require an application database.")
    if collection_service is not None and instance_id is not None and collection_service.instance_id != instance_id:
        raise ValueError("Collection service instance ID does not match server instance ID.")
    resolved_instance_id = (
        collection_service.instance_id if collection_service is not None else instance_id or str(uuid.uuid4())
    )
    if app_database is not None and collection_service is None:
        collection_service = CollectionService(
            app_database,
            instance_id=resolved_instance_id,
            clock=lambda: datetime.now(timezone.utc),
            handoff_ttl=timedelta(minutes=10),
            lease_ttl=timedelta(minutes=30),
        )
    ui_index = write_local_ui(kb, repo_root=repo_root)
    server = VoiceVaultServer((host, port), _VoiceVaultHandler)
    server.kb = kb
    server.repo_root = repo_root.resolve() if repo_root else None
    server.ui_index = ui_index
    server.ui_data = ui_index.with_name("data.json")
    prototype_root = (
        (repo_root.resolve() if repo_root else Path(__file__).resolve().parents[2])
        / "designs"
        / "voicevault-local-rag-mvp"
    )
    server.resource_ui_assets = (
        {
            "/": (prototype_root / "VoiceVault Local RAG Prototype.html", "text/html; charset=utf-8"),
            "/index.html": (prototype_root / "VoiceVault Local RAG Prototype.html", "text/html; charset=utf-8"),
            "/styles.css": (prototype_root / "styles.css", "text/css; charset=utf-8"),
            "/resource-api.js": (prototype_root / "resource-api.js", "application/javascript; charset=utf-8"),
            "/components.jsx": (prototype_root / "components.jsx", "application/javascript; charset=utf-8"),
            "/resource-ui.jsx": (prototype_root / "resource-ui.jsx", "application/javascript; charset=utf-8"),
        }
        if app_database is not None
        else {}
    )
    server.retrieval_executor = None
    server.question_executor = None
    server.index_executor = None
    server._retrieval_executor_closed = False
    server._question_executor_closed = False
    server._index_executor_closed = False
    if app_database is not None:
        if retrieval_service is None:
            try:
                embedding_provider = OpenAICompatibleEmbeddingProvider.from_environment()
            except EmbeddingUnavailable:
                embedding_provider = None
            retrieval_service = RetrievalService(
                app_database,
                RetrievalRepository(),
                LocalFullTextIndexProvider(app_database.path.parent),
                LocalVectorIndexProvider(app_database.path.parent),
                embedding_provider,
                clock=lambda: datetime.now(timezone.utc),
            )
        retrieval_service.reconcile_incomplete()
        if retrieval_executor is None:
            retrieval_executor = _ThreadedRetrievalExecutor(retrieval_service)
        server.retrieval_executor = retrieval_executor
        if index_job_service is None:
            index_job_service = IndexJobService(
                app_database,
                IndexService(
                    app_database,
                    retrieval_service.fulltext_provider,
                    retrieval_service.vector_provider,
                    retrieval_service.embedding_provider,
                    clock=lambda: datetime.now(timezone.utc),
                ),
                clock=lambda: datetime.now(timezone.utc),
            )
        index_job_service.reconcile_incomplete()
        if index_executor is None:
            index_executor = _ThreadedIndexExecutor(index_job_service)
        server.index_executor = index_executor
        if question_service is None:
            providers = {}
            try:
                answer_provider = OpenAICompatibleAnswerProvider.from_environment()
            except ProviderUnavailable:
                answer_provider = None
            if answer_provider is not None:
                providers["openai_compatible"] = answer_provider
            question_service = QuestionService(
                app_database,
                QuestionRepository(),
                providers=providers,
                clock=lambda: datetime.now(timezone.utc),
            )
        question_service.reconcile_incomplete()
        if question_executor is None and "openai_compatible" in question_service.providers:
            question_executor = _ThreadedQuestionExecutor(question_service)
        server.question_executor = question_executor
        server.api_router = ApiRouter(
            app_database,
            collection_service=collection_service,
            retrieval_service=retrieval_service,
            retrieval_executor=retrieval_executor,
            question_service=question_service,
            question_executor=question_executor,
            index_job_service=index_job_service,
            index_executor=index_executor,
        )
    else:
        server.api_router = None
    server.collection_service = collection_service
    server.instance_id = resolved_instance_id
    server.runtime_registry = runtime_registry
    server._runtime_cleared = False
    if runtime_registry is not None:
        bound_host, bound_port = server.server_address[:2]
        runtime_registry.publish(
            RuntimeRecord(
                schema_version=1,
                instance_id=resolved_instance_id,
                base_url=f"http://{bound_host}:{bound_port}",
                pid=os.getpid(),
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            )
        )
    return server


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class _VoiceVaultHandler(BaseHTTPRequestHandler):
    server: VoiceVaultServer

    def do_GET(self) -> None:
        if self._dispatch_resource_api("GET"):
            return
        asset = self.server.resource_ui_assets.get(self.path)
        if asset is not None:
            self._send_file(*asset)
            return
        if self.path in {"/", "/index.html"}:
            self._send_file(self.server.ui_index, "text/html; charset=utf-8")
            return
        if self.path == "/data.json":
            self._send_file(self.server.ui_data, "application/json; charset=utf-8")
            return
        if self.path == "/api/status":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "product": {
                        "chinese_name": "声迹",
                        "english_name": "VoiceVault",
                        "repository": "public-voice-archive",
                        "version": __version__,
                    },
                    "knowledge_base": str(self.server.kb.root),
                    "repo_root": str(self.server.repo_root) if self.server.repo_root else "",
                    "ui": {
                        "index_html": str(self.server.ui_index),
                        "data_json": str(self.server.ui_data),
                    },
                },
            )
            return
        if self.path == "/api/evaluations/answer-suite/export":
            self._handle_answer_regression_suite_export()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self._dispatch_resource_api("POST"):
            return
        if self.path == "/api/answer":
            self._handle_answer()
            return
        if self.path == "/api/compare":
            self._handle_compare()
            return
        if self.path == "/api/role/distill":
            self._handle_role_distill()
            return
        if self.path == "/api/role/ask":
            self._handle_role_ask()
            return
        if self.path in {"/api/comparison/review", "/api/compare/review"}:
            self._handle_comparison_review()
            return
        if self.path == "/api/action-runs/retry":
            self._handle_action_run_retry()
            return
        if self.path == "/api/evaluations/answer-question":
            self._handle_answer_regression_question_upsert()
            return
        if self.path == "/api/evaluations/answer-question/delete":
            self._handle_answer_regression_question_delete()
            return
        if self.path == "/api/evaluations/answer-suite/import":
            self._handle_answer_regression_suite_import()
            return
        if self.path == "/api/onboarding/role-source":
            self._handle_onboarding_role_source()
            return
        if self.path == "/api/onboarding/statement":
            self._handle_onboarding_statement()
            return
        if self.path == "/api/accounts/create":
            self._handle_account_create()
            return
        if self.path == "/api/accounts/collect":
            self._handle_account_collect()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _handle_answer(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        query = str(payload.get("query") or "").strip()
        if not query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "query is required"})
            return
        limit = _safe_limit(payload.get("limit"))
        requested_role_id = str(payload.get("role_id") or payload.get("role") or "").strip()
        auto_route = bool(payload.get("auto_route")) or requested_role_id == "__auto__"
        role_routing = None
        selected_role_id = requested_role_id if not auto_route else ""
        selection_mode = "explicit" if selected_role_id else "all"
        if auto_route:
            role_routing = suggest_roles(
                self.server.kb,
                query,
                symbol=str(payload.get("symbol") or "").strip(),
                topic=str(payload.get("topic") or "").strip(),
                limit=5,
            )
            selected_role_id = str(role_routing.get("suggested_role_id") or "")
            selection_mode = "auto" if selected_role_id else "auto_no_match"
        symbol = str(payload.get("symbol") or "").strip()
        topic = str(payload.get("topic") or "").strip()
        action_payload = {
            "query": query,
            "role_id": selected_role_id,
            "requested_role_id": requested_role_id,
            "auto_route": auto_route,
            "selection_mode": selection_mode,
            "symbol": symbol,
            "topic": topic,
            "limit": limit,
        }
        try:
            result = answer_query(
                self.server.kb,
                query,
                role_id=selected_role_id,
                symbol=symbol,
                topic=topic,
                limit=limit,
            )
            if role_routing is not None:
                result["role_routing"] = role_routing
                result["selected_role_id"] = selected_role_id
                result["selection_mode"] = selection_mode
                result["filters"]["role_id"] = selected_role_id
                result["filters"]["auto_route"] = True
            output = write_answer_outputs(default_answer_dir(self.server.kb, query), result)
        except Exception as exc:
            action_run = record_action_run(
                self.server.kb,
                action_type="answer",
                status="failed",
                payload=action_payload,
                error=str(exc),
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "action_run": action_run})
            return
        action_run = record_action_run(
            self.server.kb,
            action_type="answer",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "answer",
                "artifact_path": str(output["answer_json"]),
                "artifact_markdown": str(output["answer_markdown"]),
                "evidence_count": int(result.get("coverage", {}).get("evidence_count") or 0),
                "selected_role_id": selected_role_id,
            },
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "answer": result,
                "role_routing": role_routing,
                "selected_role_id": selected_role_id,
                "selection_mode": selection_mode,
                "answer_json": str(output["answer_json"]),
                "answer_markdown": str(output["answer_markdown"]),
                "action_run": action_run,
                "ui": self._refresh_ui(),
            },
        )

    def _handle_compare(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        query = str(payload.get("query") or "").strip()
        if not query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "query is required"})
            return
        requested_role_id = str(payload.get("role_id") or payload.get("role") or "").strip()
        if payload.get("roles"):
            roles = str(payload.get("roles") or "auto").strip()
        elif bool(payload.get("auto_route")) or requested_role_id == "__auto__":
            roles = "auto"
        elif requested_role_id:
            roles = "all" if requested_role_id == "all" else requested_role_id
        else:
            roles = "auto"
        symbol = str(payload.get("symbol") or "").strip()
        topic = str(payload.get("topic") or "").strip()
        limit = _safe_limit(payload.get("limit"), default=3)
        evidence_limit = _safe_limit(payload.get("evidence_limit") or payload.get("evidenceLimit"), default=3)
        action_payload = {
            "query": query,
            "roles": roles,
            "symbol": symbol,
            "topic": topic,
            "limit": limit,
            "evidence_limit": evidence_limit,
        }
        try:
            result = compare_roles(
                self.server.kb,
                query,
                roles=roles,
                symbol=symbol,
                topic=topic,
                limit=limit,
                evidence_limit=evidence_limit,
            )
            output = write_comparison_outputs(default_comparison_dir(self.server.kb, query), result)
        except Exception as exc:
            action_run = record_action_run(
                self.server.kb,
                action_type="compare",
                status="failed",
                payload=action_payload,
                error=str(exc),
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "action_run": action_run})
            return
        action_run = record_action_run(
            self.server.kb,
            action_type="compare",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "comparison",
                "artifact_path": str(output["comparison_json"]),
                "artifact_markdown": str(output["comparison_markdown"]),
                "evidence_count": int(result.get("coverage", {}).get("evidence_count") or 0),
                "role_count": int(result.get("coverage", {}).get("role_count") or 0),
                "review_status": str(result.get("review", {}).get("status") or "draft"),
            },
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "comparison": result,
                "comparison_json": str(output["comparison_json"]),
                "comparison_markdown": str(output["comparison_markdown"]),
                "action_run": action_run,
                "ui": self._refresh_ui(),
            },
        )

    def _handle_role_distill(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        role_id = str(payload.get("role_id") or payload.get("role") or "").strip()
        if not role_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "role_id is required"})
            return
        try:
            skill = distill_role_skill(self.server.kb, role_id, limit=_safe_limit(payload.get("limit"), default=12))
            output = write_role_skill(self.server.kb, skill)
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "skill": skill,
                "skill_json": str(output["skill_json"]),
                "skill_markdown": str(output["skill_markdown"]),
                "ui": self._refresh_ui(),
            },
        )

    def _handle_role_ask(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        role_id = str(payload.get("role_id") or payload.get("role") or "").strip()
        query = str(payload.get("query") or "").strip()
        if not role_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "role_id is required"})
            return
        if not query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "query is required"})
            return
        action_payload = {
            "role_id": role_id,
            "query": query,
            "symbol": str(payload.get("symbol") or "").strip(),
            "topic": str(payload.get("topic") or "").strip(),
            "limit": _safe_limit(payload.get("limit")),
            "dry_run": _truthy(payload.get("dry_run"), default=True),
            "model": str(payload.get("model") or "").strip(),
            "temperature": _safe_temperature(payload.get("temperature")),
        }
        try:
            result = ask_role_agent(
                self.server.kb,
                role_id,
                query,
                symbol=action_payload["symbol"],
                topic=action_payload["topic"],
                limit=action_payload["limit"],
                dry_run=action_payload["dry_run"],
                model=action_payload["model"],
                temperature=action_payload["temperature"],
            )
        except Exception as exc:
            action_run = record_action_run(
                self.server.kb,
                action_type="role_agent",
                status="failed",
                payload=action_payload,
                error=str(exc),
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc), "action_run": action_run})
            return
        action_run = record_action_run(
            self.server.kb,
            action_type="role_agent",
            status="completed" if result.get("ok") else "failed",
            payload=action_payload,
            result={
                "artifact_kind": "role_agent",
                "artifact_path": result.get("role_agent_json", ""),
                "artifact_markdown": result.get("role_agent_markdown", ""),
                "llm_status": str(result.get("llm", {}).get("status") if isinstance(result.get("llm"), dict) else ""),
            },
            error="" if result.get("ok") else _role_agent_error(result),
        )
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
        self._send_json(status, {"ok": bool(result.get("ok")), **result, "action_run": action_run, "ui": self._refresh_ui()})

    def _handle_comparison_review(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        query = str(payload.get("query") or "").strip()
        path_value = str(payload.get("path") or "").strip()
        if not query and not path_value:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "query or path is required"})
            return
        comparison_path = Path(path_value) if path_value else default_comparison_dir(self.server.kb, query) / "comparison.json"
        action_payload = {
            "query": query,
            "path": path_value,
            "status": str(payload.get("status") or "").strip(),
            "reviewer": str(payload.get("reviewer") or "manual").strip(),
            "notes": str(payload.get("notes") or "").strip(),
        }
        try:
            result = review_comparison_export(
                comparison_path,
                status=action_payload["status"],
                reviewer=action_payload["reviewer"],
                notes=action_payload["notes"],
            )
        except ValueError as exc:
            action_run = record_action_run(
                self.server.kb,
                action_type="comparison_review",
                status="failed",
                payload=action_payload,
                error=str(exc),
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc), "action_run": action_run})
            return
        action_run = record_action_run(
            self.server.kb,
            action_type="comparison_review",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "comparison",
                "artifact_path": str(result.get("comparison_json") or comparison_path),
                "artifact_markdown": str(result.get("comparison_markdown") or ""),
                "review_status": str(result.get("comparison", {}).get("review", {}).get("status") or ""),
            },
        )
        self._send_json(HTTPStatus.OK, {"ok": True, **result, "action_run": action_run, "ui": self._refresh_ui()})

    def _handle_action_run_retry(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        run_id = str(payload.get("run_id") or payload.get("runId") or "").strip()
        run = read_action_run(self.server.kb, run_id)
        if run is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "action run not found"})
            return
        if not run.get("retryable"):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "action run is not retryable", "retried_from": run},
            )
            return
        action_type = run["action_type"]
        if action_type == "answer":
            status, response = self._retry_answer_action(run)
        elif action_type == "compare":
            status, response = self._retry_compare_action(run)
        elif action_type == "comparison_review":
            status, response = self._retry_comparison_review_action(run)
        else:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": f"unsupported retry action type: {action_type}", "retried_from": run},
            )
            return
        self._send_json(status, {"retried_from": run, **response})

    def _retry_answer_action(self, run: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        action_payload = dict(run.get("payload") or {})
        action_payload["retry_of"] = run["run_id"]
        query = str(action_payload.get("query") or "").strip()
        if not query:
            return self._record_retry_failure("answer", action_payload, "query is required", HTTPStatus.BAD_REQUEST)
        limit = _safe_limit(action_payload.get("limit"))
        requested_role_id = str(action_payload.get("requested_role_id") or action_payload.get("role_id") or "").strip()
        auto_route = _truthy(action_payload.get("auto_route")) or requested_role_id == "__auto__"
        selected_role_id = str(action_payload.get("role_id") or "").strip() if not auto_route else ""
        selection_mode = str(action_payload.get("selection_mode") or ("explicit" if selected_role_id else "all")).strip()
        role_routing = None
        symbol = str(action_payload.get("symbol") or "").strip()
        topic = str(action_payload.get("topic") or "").strip()
        if auto_route:
            role_routing = suggest_roles(self.server.kb, query, symbol=symbol, topic=topic, limit=5)
            selected_role_id = str(role_routing.get("suggested_role_id") or "")
            selection_mode = "auto" if selected_role_id else "auto_no_match"
        action_payload.update(
            {
                "query": query,
                "role_id": selected_role_id,
                "requested_role_id": requested_role_id,
                "auto_route": auto_route,
                "selection_mode": selection_mode,
                "symbol": symbol,
                "topic": topic,
                "limit": limit,
            }
        )
        try:
            result = answer_query(
                self.server.kb,
                query,
                role_id=selected_role_id,
                symbol=symbol,
                topic=topic,
                limit=limit,
            )
            if role_routing is not None:
                result["role_routing"] = role_routing
                result["selected_role_id"] = selected_role_id
                result["selection_mode"] = selection_mode
                result["filters"]["role_id"] = selected_role_id
                result["filters"]["auto_route"] = True
            output = write_answer_outputs(default_answer_dir(self.server.kb, query), result)
        except Exception as exc:
            return self._record_retry_failure("answer", action_payload, str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        action_run = record_action_run(
            self.server.kb,
            action_type="answer",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "answer",
                "artifact_path": str(output["answer_json"]),
                "artifact_markdown": str(output["answer_markdown"]),
                "evidence_count": int(result.get("coverage", {}).get("evidence_count") or 0),
                "selected_role_id": selected_role_id,
                "retry_of": run["run_id"],
            },
            source="action_retry",
        )
        resolve_action_run(self.server.kb, run["run_id"], resolved_by=action_run["run_id"])
        return (
            HTTPStatus.OK,
            {
                "ok": True,
                "answer": result,
                "role_routing": role_routing,
                "selected_role_id": selected_role_id,
                "selection_mode": selection_mode,
                "answer_json": str(output["answer_json"]),
                "answer_markdown": str(output["answer_markdown"]),
                "action_run": action_run,
                "ui": self._refresh_ui(),
            },
        )

    def _retry_compare_action(self, run: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        action_payload = dict(run.get("payload") or {})
        action_payload["retry_of"] = run["run_id"]
        query = str(action_payload.get("query") or "").strip()
        if not query:
            return self._record_retry_failure("compare", action_payload, "query is required", HTTPStatus.BAD_REQUEST)
        roles = str(action_payload.get("roles") or "auto").strip() or "auto"
        symbol = str(action_payload.get("symbol") or "").strip()
        topic = str(action_payload.get("topic") or "").strip()
        limit = _safe_limit(action_payload.get("limit"), default=3)
        evidence_limit = _safe_limit(action_payload.get("evidence_limit") or action_payload.get("evidenceLimit"), default=3)
        action_payload.update(
            {
                "query": query,
                "roles": roles,
                "symbol": symbol,
                "topic": topic,
                "limit": limit,
                "evidence_limit": evidence_limit,
            }
        )
        try:
            result = compare_roles(
                self.server.kb,
                query,
                roles=roles,
                symbol=symbol,
                topic=topic,
                limit=limit,
                evidence_limit=evidence_limit,
            )
            output = write_comparison_outputs(default_comparison_dir(self.server.kb, query), result)
        except Exception as exc:
            return self._record_retry_failure("compare", action_payload, str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
        action_run = record_action_run(
            self.server.kb,
            action_type="compare",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "comparison",
                "artifact_path": str(output["comparison_json"]),
                "artifact_markdown": str(output["comparison_markdown"]),
                "evidence_count": int(result.get("coverage", {}).get("evidence_count") or 0),
                "role_count": int(result.get("coverage", {}).get("role_count") or 0),
                "review_status": str(result.get("review", {}).get("status") or "draft"),
                "retry_of": run["run_id"],
            },
            source="action_retry",
        )
        resolve_action_run(self.server.kb, run["run_id"], resolved_by=action_run["run_id"])
        return (
            HTTPStatus.OK,
            {
                "ok": True,
                "comparison": result,
                "comparison_json": str(output["comparison_json"]),
                "comparison_markdown": str(output["comparison_markdown"]),
                "action_run": action_run,
                "ui": self._refresh_ui(),
            },
        )

    def _retry_comparison_review_action(self, run: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
        action_payload = dict(run.get("payload") or {})
        action_payload["retry_of"] = run["run_id"]
        query = str(action_payload.get("query") or "").strip()
        path_value = str(action_payload.get("path") or "").strip()
        if not query and not path_value:
            return self._record_retry_failure(
                "comparison_review",
                action_payload,
                "query or path is required",
                HTTPStatus.BAD_REQUEST,
            )
        comparison_path = Path(path_value) if path_value else default_comparison_dir(self.server.kb, query) / "comparison.json"
        action_payload.update(
            {
                "query": query,
                "path": path_value,
                "status": str(action_payload.get("status") or "").strip(),
                "reviewer": str(action_payload.get("reviewer") or "manual").strip(),
                "notes": str(action_payload.get("notes") or "").strip(),
            }
        )
        try:
            result = review_comparison_export(
                comparison_path,
                status=action_payload["status"],
                reviewer=action_payload["reviewer"],
                notes=action_payload["notes"],
            )
        except ValueError as exc:
            return self._record_retry_failure("comparison_review", action_payload, str(exc), HTTPStatus.BAD_REQUEST)
        action_run = record_action_run(
            self.server.kb,
            action_type="comparison_review",
            status="completed",
            payload=action_payload,
            result={
                "artifact_kind": "comparison",
                "artifact_path": str(result.get("comparison_json") or comparison_path),
                "artifact_markdown": str(result.get("comparison_markdown") or ""),
                "review_status": str(result.get("comparison", {}).get("review", {}).get("status") or ""),
                "retry_of": run["run_id"],
            },
            source="action_retry",
        )
        resolve_action_run(self.server.kb, run["run_id"], resolved_by=action_run["run_id"])
        return HTTPStatus.OK, {"ok": True, **result, "action_run": action_run, "ui": self._refresh_ui()}

    def _record_retry_failure(
        self,
        action_type: str,
        action_payload: dict[str, Any],
        error: str,
        status: HTTPStatus,
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        action_run = record_action_run(
            self.server.kb,
            action_type=action_type,
            status="failed",
            payload=action_payload,
            error=error,
            source="action_retry",
        )
        return status, {"ok": False, "error": error, "action_run": action_run, "ui": self._refresh_ui()}

    def _handle_answer_regression_question_upsert(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        try:
            result = upsert_answer_regression_question(self.server.kb, payload)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                **result,
                "audit": audit_answer_regression(self.server.kb),
                "changes": load_answer_regression_changelog(self.server.kb),
                "ui": self._refresh_ui(),
            },
        )

    def _handle_answer_regression_question_delete(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        question_id = str(payload.get("id") or payload.get("question_id") or payload.get("questionId") or "").strip()
        updated_by = str(payload.get("updated_by") or payload.get("updatedBy") or "local-ui").strip() or "local-ui"
        try:
            result = delete_answer_regression_question(self.server.kb, question_id, updated_by=updated_by)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                **result,
                "audit": audit_answer_regression(self.server.kb),
                "changes": load_answer_regression_changelog(self.server.kb),
                "ui": self._refresh_ui(),
            },
        )

    def _handle_answer_regression_suite_export(self) -> None:
        result = export_answer_regression_suite(self.server.kb)
        self._send_json(HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST, result)

    def _handle_answer_regression_suite_import(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        suite_payload = payload.get("suite") if isinstance(payload.get("suite"), dict) else payload
        dry_run = _truthy(payload.get("dry_run", True))
        updated_by = str(payload.get("updated_by") or payload.get("updatedBy") or "local-ui").strip() or "local-ui"
        result = import_answer_regression_suite(
            self.server.kb,
            suite_payload,
            dry_run=dry_run,
            updated_by=updated_by,
        )
        status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST
        self._send_json(
            status,
            {
                **result,
                "audit": audit_answer_regression(self.server.kb),
                "changes": load_answer_regression_changelog(self.server.kb),
                "ui": self._refresh_ui(),
            },
        )

    def _handle_onboarding_role_source(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        try:
            result = create_public_role_source(
                self.server.kb,
                role_id=str(payload.get("role_id") or payload.get("role") or "").strip(),
                source_id=str(payload.get("source_id") or payload.get("source") or "").strip(),
                platform=str(payload.get("platform") or "").strip(),
                display_name=str(payload.get("display_name") or payload.get("name") or "").strip(),
                source_url=str(payload.get("source_url") or payload.get("url") or "").strip(),
                symbols=_string_list(payload.get("symbols")),
                topics=_string_list(payload.get("topics")),
                tags=_string_list(payload.get("tags")),
                notes=str(payload.get("notes") or "").strip(),
                overwrite=_truthy(payload.get("overwrite")),
            )
        except (FileExistsError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, **result, "ui": self._refresh_ui()})

    def _handle_onboarding_statement(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        try:
            result = ingest_public_statement(
                self.server.kb,
                source_id=str(payload.get("source_id") or payload.get("source") or "").strip(),
                text=str(payload.get("text") or payload.get("statement") or "").strip(),
                title=str(payload.get("title") or "").strip(),
                source_url=str(payload.get("source_url") or payload.get("url") or "").strip(),
                published_at=str(payload.get("published_at") or "").strip(),
                captured_at=str(payload.get("captured_at") or "").strip(),
                symbols=_string_list(payload.get("symbols")),
                topics=_string_list(payload.get("topics")),
                stance=str(payload.get("stance") or "unclear").strip(),
                time_horizon=str(payload.get("time_horizon") or payload.get("timeHorizon") or "unknown").strip(),
                confidence=str(payload.get("confidence") or "low").strip(),
                notes=str(payload.get("notes") or "").strip(),
                sync=_truthy(payload.get("sync"), default=True),
                archive=_truthy(payload.get("archive"), default=True),
                generate=_payload_bool(payload, "generate_profile", "generateProfile", default=True),
                promote=_payload_bool(payload, "promote_profile", "promoteProfile", default=False),
                overwrite_profile=_payload_bool(payload, "overwrite_profile", "overwriteProfile", default=True),
                reviewer=str(payload.get("reviewer") or "local-ui").strip(),
                review_note=str(payload.get("review_note") or payload.get("reviewNote") or "").strip(),
            )
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, **result, "ui": self._refresh_ui()})

    def _handle_account_create(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        try:
            account = create_account(
                self.server.kb,
                account_id=str(payload.get("account_id") or payload.get("account") or "").strip(),
                platform=str(payload.get("platform") or "").strip(),
                platform_account_id=str(payload.get("platform_account_id") or payload.get("platformAccountId") or "").strip(),
                role_id=str(payload.get("role_id") or payload.get("role") or "").strip(),
                source_id=str(payload.get("source_id") or payload.get("source") or "").strip(),
                source_url=str(payload.get("source_url") or payload.get("sourceUrl") or "").strip(),
                display_name=str(payload.get("display_name") or payload.get("displayName") or "").strip(),
                collection_mode=str(payload.get("collection_mode") or payload.get("mode") or "auto").strip(),
                feed_url=str(payload.get("feed_url") or payload.get("feedUrl") or "").strip(),
                input_path=str(payload.get("input_path") or payload.get("inputPath") or "").strip(),
                api_url=str(payload.get("api_url") or payload.get("apiUrl") or "").strip(),
                adapter_config=payload.get("adapter_config") if isinstance(payload.get("adapter_config"), dict) else {},
                symbols=_string_list(payload.get("symbols")),
                topics=_string_list(payload.get("topics")),
                tags=_string_list(payload.get("tags")),
                notes=str(payload.get("notes") or "").strip(),
                enabled=not _truthy(payload.get("disabled")),
                overwrite=_truthy(payload.get("overwrite")),
            )
        except (FileExistsError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "account": account, "account_status": read_account_status(self.server.kb), "ui": self._refresh_ui()},
        )

    def _handle_account_collect(self) -> None:
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must be a JSON object"})
            return
        try:
            result = collect_account(
                self.server.kb,
                str(payload.get("account_id") or payload.get("account") or "").strip(),
                dry_run=_truthy(payload.get("dry_run") or payload.get("dryRun")),
                sync=_truthy(payload.get("sync"), default=True),
                archive=_truthy(payload.get("archive")),
            )
        except (FileNotFoundError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        status = HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST
        self._send_json(
            status,
            {
                "ok": result["ok"],
                "account_collection": result,
                "account_status": read_account_status(self.server.kb),
                "ui": self._refresh_ui(),
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _dispatch_resource_api(self, method: str) -> bool:
        router = self.server.api_router
        if router is None:
            return False
        if not router.owns(self.path):
            return False
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        body = None
        if method == "POST" and length > 0:
            if length <= MAX_JSON_BYTES:
                body = self.rfile.read(length)
            else:
                self.close_connection = True
                self._discard_oversize_body()
        try:
            response = router.dispatch(method, self.path, body=body, content_length=length)
        except InvalidJsonError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": {"code": "invalid_json", "message": "Request body is not valid JSON."}})
            return True
        if response is None:
            return False
        self._send_json(response.status, response.payload)
        return True

    def _discard_oversize_body(self) -> None:
        remaining = MAX_JSON_BYTES + 1
        deadline = time.monotonic() + 0.25
        original_timeout = self.connection.gettimeout()
        try:
            while remaining > 0:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                try:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                except (TimeoutError, socket.timeout, OSError):
                    break
                if not chunk:
                    break
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(original_timeout)

    def _read_json_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_JSON_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "file not found"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _refresh_ui(self) -> dict[str, Any]:
        self.server.ui_index = write_local_ui(self.server.kb, repo_root=self.server.repo_root)
        self.server.ui_data = self.server.ui_index.with_name("data.json")
        try:
            data = json.loads(self.server.ui_data.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return {
            "index_html": str(self.server.ui_index),
            "data_json": str(self.server.ui_data),
            "data": data,
        }


def _safe_limit(value: Any, *, default: int = 5) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 20))


def _safe_temperature(value: Any, *, default: float = 0.2) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(parsed, 2.0))


def _role_agent_error(result: dict[str, Any]) -> str:
    llm = result.get("llm")
    if isinstance(llm, dict):
        error = str(llm.get("error") or "").strip()
        if error:
            return error
        status = str(llm.get("status") or "").strip()
        if status:
            return f"Role Agent LLM call failed with status={status}"
    return "Role Agent LLM call failed"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _payload_bool(payload: dict[str, Any], snake_key: str, camel_key: str, *, default: bool) -> bool:
    if snake_key in payload:
        return _truthy(payload.get(snake_key), default=default)
    if camel_key in payload:
        return _truthy(payload.get(camel_key), default=default)
    return default
