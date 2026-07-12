from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from voicevault.embedding import EmbeddingBatch, FakeEmbeddingProvider
from voicevault.vector_index import (
    LocalVectorIndexProvider,
    VectorIndexConflict,
    VectorIndexInvalid,
)


GENERATION_A = "11111111-1111-4111-8111-111111111111"
GENERATION_B = "22222222-2222-4222-8222-222222222222"
PERSON_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PERSON_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def batch(
    vectors: tuple[tuple[float, ...], ...],
    *,
    model: str = "model-a",
    fingerprint: str = "a" * 64,
) -> EmbeddingBatch:
    return EmbeddingBatch(
        model=model,
        dimension=len(vectors[0]),
        provider_fingerprint=fingerprint,
        vectors=vectors,
    )


class LocalVectorIndexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = Path(self.temp_dir.name)
        self.provider = LocalVectorIndexProvider(self.data_dir)

    def test_build_writes_normalized_little_endian_float32_and_exact_manifest(self) -> None:
        relative = self.provider.build_person_shard(
            GENERATION_A,
            PERSON_A,
            ("chunk-a", "chunk-b"),
            batch(((3.0, 4.0), (0.0, -2.0))),
        )

        manifest_path = self.data_dir / relative
        vector_path = manifest_path.with_suffix(".f32")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = vector_path.read_bytes()
        matrix = np.frombuffer(raw, dtype="<f4").reshape(2, 2)

        self.assertEqual(
            relative,
            f"indexes/generations/{GENERATION_A}/persons/{PERSON_A}.json",
        )
        self.assertEqual(matrix.dtype, np.dtype("<f4"))
        np.testing.assert_allclose(matrix, ((0.6, 0.8), (0.0, -1.0)))
        self.assertEqual(manifest["schema_version"], "voicevault-vector-v1")
        self.assertEqual(manifest["generation_id"], GENERATION_A)
        self.assertEqual(manifest["person_id"], PERSON_A)
        self.assertEqual(manifest["model"], "model-a")
        self.assertEqual(manifest["dimension"], 2)
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(manifest["chunk_ids"], ["chunk-a", "chunk-b"])
        self.assertEqual(manifest["vector_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["provider_fingerprint"], "a" * 64)
        self.assertIs(manifest["normalized"], True)

        loaded = self.provider.load_person_shard(GENERATION_A, PERSON_A)
        self.assertEqual(loaded.chunk_ids, ("chunk-a", "chunk-b"))
        self.assertEqual(loaded.provider_fingerprint, "a" * 64)

    def test_cosine_ranking_has_stable_ties_and_allowed_chunk_filter(self) -> None:
        self.provider.build_person_shard(
            GENERATION_A,
            PERSON_A,
            ("chunk-b", "chunk-a", "chunk-c"),
            batch(((1.0, 0.0), (1.0, 0.0), (0.0, 1.0))),
        )

        hits = self.provider.search_person(GENERATION_A, PERSON_A, (1.0, 0.0), 10)
        filtered = self.provider.search_person(
            GENERATION_A,
            PERSON_A,
            (1.0, 0.0),
            10,
            allowed_chunk_ids=("chunk-c", "chunk-b"),
        )

        self.assertEqual([hit.chunk_id for hit in hits], ["chunk-a", "chunk-b", "chunk-c"])
        self.assertEqual([hit.rank for hit in hits], [1, 2, 3])
        self.assertAlmostEqual(hits[0].similarity, 1.0)
        self.assertEqual([hit.chunk_id for hit in filtered], ["chunk-b", "chunk-c"])

    def test_search_memmaps_only_selected_person_and_rejects_bad_queries(self) -> None:
        for person_id, vector in ((PERSON_A, (1.0, 0.0)), (PERSON_B, (0.0, 1.0))):
            self.provider.build_person_shard(
                GENERATION_A, person_id, (f"chunk-{person_id[0]}",), batch((vector,))
            )
        opened: list[str] = []
        real_memmap = np.memmap

        def recording_memmap(filename, *args, **kwargs):
            opened.append(os.fspath(filename))
            return real_memmap(filename, *args, **kwargs)

        with patch("voicevault.vector_index.np.memmap", side_effect=recording_memmap):
            self.provider.search_person(GENERATION_A, PERSON_A, (1.0, 0.0), 5)

        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].endswith(f"{PERSON_A}.f32"))
        for query in ((0.0, 0.0), (1.0,), (float("nan"), 0.0)):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.provider.search_person(GENERATION_A, PERSON_A, query, 5)

    def test_identical_pair_is_reused_and_conflicting_pair_is_never_overwritten(self) -> None:
        embeddings = batch(((1.0, 2.0),))
        relative = self.provider.build_person_shard(
            GENERATION_A, PERSON_A, ("chunk-a",), embeddings
        )
        manifest_path = self.data_dir / relative
        vector_path = manifest_path.with_suffix(".f32")
        before = {
            path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
            for path in (manifest_path, vector_path)
        }

        self.assertEqual(
            self.provider.build_person_shard(
                GENERATION_A, PERSON_A, ("chunk-a",), embeddings
            ),
            relative,
        )
        self.assertEqual(
            {
                path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
                for path in (manifest_path, vector_path)
            },
            before,
        )
        with self.assertRaises(VectorIndexConflict):
            self.provider.build_person_shard(
                GENERATION_A, PERSON_A, ("chunk-a",), batch(((2.0, 1.0),))
            )
        self.assertEqual(vector_path.read_bytes(), before[vector_path][2])

    def test_interrupted_half_pair_can_be_completed_without_deletion(self) -> None:
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("interrupted publish")
            return real_link(source, destination)

        with patch("voicevault.vector_index.os.link", side_effect=fail_second_link):
            with self.assertRaises(VectorIndexInvalid):
                self.provider.build_person_shard(
                    GENERATION_A, PERSON_A, ("chunk-a",), batch(((1.0, 0.0),))
                )

        person_dir = self.data_dir / "indexes" / "generations" / GENERATION_A / "persons"
        self.assertTrue((person_dir / f"{PERSON_A}.f32").is_file())
        self.assertFalse((person_dir / f"{PERSON_A}.json").exists())
        relative = self.provider.build_person_shard(
            GENERATION_A, PERSON_A, ("chunk-a",), batch(((1.0, 0.0),))
        )
        self.assertTrue((self.data_dir / relative).is_file())
        self.assertTrue(tuple(person_dir.glob("*.staging.*")))

    def test_corrupt_manifest_and_unsafe_identifiers_are_sanitized(self) -> None:
        relative = self.provider.build_person_shard(
            GENERATION_A, PERSON_A, ("chunk-a",), batch(((1.0, 0.0),))
        )
        manifest_path = self.data_dir / relative
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["vector_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(VectorIndexInvalid) as raised:
            self.provider.load_person_shard(GENERATION_A, PERSON_A)
        self.assertNotIn(str(self.data_dir), str(raised.exception))
        for generation_id, person_id in (
            ("../escape", PERSON_A),
            (GENERATION_A, "../escape"),
            ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", PERSON_A),
        ):
            with self.subTest(generation_id=generation_id, person_id=person_id), self.assertRaises(ValueError):
                self.provider.search_person(generation_id, person_id, (1.0, 0.0), 5)


if __name__ == "__main__":
    unittest.main()
