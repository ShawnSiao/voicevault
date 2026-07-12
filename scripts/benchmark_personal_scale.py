from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from voicevault.app_db import AppDatabase  # noqa: E402
from voicevault.embedding import FakeEmbeddingProvider  # noqa: E402
from voicevault.fulltext_index import LocalFullTextIndexProvider  # noqa: E402
from voicevault.index_service import IndexService  # noqa: E402
from voicevault.retrieval import RetrievalRepository, RetrievalRequest  # noqa: E402
from voicevault.retrieval_service import RetrievalService  # noqa: E402
from voicevault.vector_index import LocalVectorIndexProvider  # noqa: E402


PERSON_COUNT = 50
POST_COUNT = 10_000
SELECTED_PERSON_COUNT = 10
SAMPLES = 7
POSTS_PER_PERSON = POST_COUNT // PERSON_COUNT
QUERY = "benchmark needle"
P95_LIMIT_MS = 5_000.0
NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat()


def _stable_uuid(namespace: str, ordinal: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicevault-benchmark:{namespace}:{ordinal}"))


def _seed_database(database: AppDatabase) -> tuple[str, ...]:
    people = tuple(_stable_uuid("person", index) for index in range(PERSON_COUNT))
    with database.connect() as connection:
        connection.execute("PRAGMA synchronous = OFF")
        connection.executemany(
            "INSERT INTO persons VALUES (?, ?, ?, ?)",
            (
                (person_id, f"Benchmark Person {index:02d}", NOW_TEXT, NOW_TEXT)
                for index, person_id in enumerate(people)
            ),
        )
        connection.executemany(
            """
            INSERT INTO platform_accounts(
                account_id, person_id, platform, external_user_id,
                archive_basis_confirmed_at, created_at, updated_at
            ) VALUES (?, ?, 'example', ?, ?, ?, ?)
            """,
            (
                (
                    f"account-{index:02d}",
                    person_id,
                    f"benchmark-{index:02d}",
                    NOW_TEXT,
                    NOW_TEXT,
                    NOW_TEXT,
                )
                for index, person_id in enumerate(people)
            ),
        )
        for person_index in range(PERSON_COUNT):
            account_id = f"account-{person_index:02d}"
            offset = person_index * POSTS_PER_PERSON
            posts = []
            revisions = []
            dispositions = []
            for local_index in range(POSTS_PER_PERSON):
                post_index = offset + local_index
                post_id = f"post-{post_index:06d}"
                revision_id = f"revision-{post_index:06d}"
                text = (
                    f"{QUERY} person {person_index:02d} post {post_index:06d} "
                    "public archive evidence"
                )
                posts.append(
                    (
                        post_id,
                        account_id,
                        f"external-{post_index:06d}",
                        NOW_TEXT,
                        f"https://example.test/{person_index:02d}/{post_index:06d}",
                        NOW_TEXT,
                    )
                )
                revisions.append(
                    (
                        revision_id,
                        post_id,
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        text,
                        NOW_TEXT,
                    )
                )
                dispositions.append((post_id, NOW_TEXT))
            connection.executemany("INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)", posts)
            connection.executemany(
                "INSERT INTO post_revisions VALUES (?, ?, ?, ?, ?, NULL)", revisions
            )
            connection.executemany(
                "INSERT INTO content_dispositions VALUES (?, 'active', NULL, ?, NULL)",
                dispositions,
            )
    return people


def _build_selected_indexes(
    database: AppDatabase,
    data_dir: Path,
    selected_people: tuple[str, ...],
) -> tuple[LocalFullTextIndexProvider, LocalVectorIndexProvider, FakeEmbeddingProvider]:
    fulltext = LocalFullTextIndexProvider(data_dir)
    vector = LocalVectorIndexProvider(data_dir)
    embedding = FakeEmbeddingProvider(dimension=8)
    index = IndexService(
        database,
        fulltext,
        vector,
        embedding,
        clock=lambda: NOW,
        batch_size=512,
    )
    for person_id in selected_people:
        result = index.rebuild_person(person_id)
        if (result.status, result.retrieval_mode) != ("ready", "hybrid"):
            raise RuntimeError("Benchmark index generation did not become hybrid-ready.")
    return fulltext, vector, embedding


def _measure(
    database: AppDatabase,
    fulltext: LocalFullTextIndexProvider,
    vector: LocalVectorIndexProvider,
    embedding: FakeEmbeddingProvider,
    selected_people: tuple[str, ...],
) -> list[float]:
    service = RetrievalService(
        database,
        RetrievalRepository(),
        fulltext,
        vector,
        embedding,
        clock=lambda: NOW,
        candidate_pool=100,
    )
    request = RetrievalRequest(
        QUERY,
        selected_people,
        limit=20,
        min_hits_per_person=1,
    )

    def run_once() -> float:
        started = time.perf_counter()
        result = service.execute(service.create_run(request).run_id)
        elapsed_ms = (time.perf_counter() - started) * 1_000
        if result.status != "succeeded" or len(result.hits) < SELECTED_PERSON_COUNT:
            raise RuntimeError("Benchmark retrieval did not return all selected people.")
        return elapsed_ms

    run_once()
    return [run_once() for _ in range(SAMPLES)]


def _percentile_nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def main() -> int:
    if PERSON_COUNT * POSTS_PER_PERSON != POST_COUNT:
        raise RuntimeError("Benchmark post distribution must be exact.")
    with tempfile.TemporaryDirectory(prefix="voicevault-personal-scale-") as temp_dir:
        data_dir = Path(temp_dir)
        database = AppDatabase(data_dir=data_dir)
        database.initialize()
        people = _seed_database(database)
        selected = people[:SELECTED_PERSON_COUNT]
        fulltext, vector, embedding = _build_selected_indexes(
            database, data_dir, selected
        )
        samples = _measure(
            database, fulltext, vector, embedding, selected
        )

    p95_ms = _percentile_nearest_rank(samples, 0.95)
    report = {
        "person_count": PERSON_COUNT,
        "post_count": POST_COUNT,
        "selected_person_count": SELECTED_PERSON_COUNT,
        "samples": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(p95_ms, 3),
        "max_ms": round(max(samples), 3),
        "passed": p95_ms < P95_LIMIT_MS,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
