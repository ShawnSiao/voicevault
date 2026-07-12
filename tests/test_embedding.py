from __future__ import annotations

import io
import json
import math
import os
import unittest
from dataclasses import FrozenInstanceError
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from voicevault.embedding import (
    EmbeddingResponseInvalid,
    EmbeddingUnavailable,
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)


API_KEY = "test-key-not-a-secret"
BASE_URL = "http://127.0.0.1:9999/v1/"
MODEL = "embedding-model-a"


class FakeResponse:
    def __init__(self, body: object, *, raw: bool = False) -> None:
        self.body = body if raw else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body  # type: ignore[return-value]


class RecordingOpener:
    def __init__(self, response: object = None, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, float]] = []

    def open(self, request, timeout: float):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.response)


def response(vectors: list[list[object]], *, model: str = MODEL, indices=None) -> dict:
    order = list(range(len(vectors))) if indices is None else indices
    return {
        "model": model,
        "data": [
            {"index": index, "embedding": vector}
            for index, vector in zip(order, vectors)
        ],
    }


def provider_for(payload: object, *, api_key: str = API_KEY):
    opener = RecordingOpener(payload)
    provider = OpenAICompatibleEmbeddingProvider.from_environment(
        {
            "VOICEVAULT_EMBEDDING_BASE_URL": BASE_URL,
            "VOICEVAULT_EMBEDDING_MODEL": MODEL,
            "VOICEVAULT_EMBEDDING_API_KEY": api_key,
        },
        opener=opener,
        timeout=7.5,
    )
    return provider, opener


class FakeEmbeddingProviderTests(unittest.TestCase):
    def test_fake_is_deterministic_deeply_immutable_records_batches_and_switches_model(self) -> None:
        provider = FakeEmbeddingProvider(model="fake-a", dimension=4)
        first = provider.embed(("alpha", "中文🙂"))
        second = provider.embed(("alpha", "中文🙂"))

        self.assertEqual(first, second)
        self.assertEqual(first.model, "fake-a")
        self.assertEqual(first.dimension, 4)
        self.assertEqual(first.provider_fingerprint, provider.fingerprint_for_dimension(4))
        self.assertEqual(provider.batches, (("alpha", "中文🙂"), ("alpha", "中文🙂")))
        self.assertIsInstance(first.vectors, tuple)
        self.assertTrue(all(isinstance(vector, tuple) for vector in first.vectors))
        self.assertTrue(all(math.isfinite(value) for vector in first.vectors for value in vector))
        with self.assertRaises(FrozenInstanceError):
            first.model = "changed"  # type: ignore[misc]

        old_fingerprint = first.provider_fingerprint
        provider.set_model("fake-b")
        switched = provider.embed(("alpha",))
        self.assertEqual(switched.model, "fake-b")
        self.assertNotEqual(switched.provider_fingerprint, old_fingerprint)
        self.assertNotEqual(switched.vectors[0], first.vectors[0])
        configured_dimension = FakeEmbeddingProvider(model="fake-b", dimension=5)
        self.assertEqual(
            configured_dimension.fingerprint_for_dimension(4),
            configured_dimension.fingerprint_for_dimension(5),
        )
        for dimension in (0, -1, True, 1.5):
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                provider.fingerprint_for_dimension(dimension)  # type: ignore[arg-type]

    def test_fake_rejects_invalid_batches_and_supports_explicit_failure(self) -> None:
        provider = FakeEmbeddingProvider(dimension=3)
        for texts in ((), ["not-a-tuple"], ("",), ("   ",), (7,)):
            with self.subTest(texts=texts), self.assertRaises(ValueError):
                provider.embed(texts)  # type: ignore[arg-type]
        provider.set_failure(True)
        with self.assertRaises(EmbeddingUnavailable):
            provider.embed(("valid",))


class OpenAICompatibleEmbeddingProviderTests(unittest.TestCase):
    def test_missing_or_invalid_configuration_is_unavailable_and_uses_only_supplied_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VOICEVAULT_EMBEDDING_BASE_URL": "https://must-not-be-read.example",
                "VOICEVAULT_EMBEDDING_MODEL": "must-not-be-read",
            },
            clear=False,
        ):
            for env in ({}, {"VOICEVAULT_EMBEDDING_BASE_URL": BASE_URL}, {"VOICEVAULT_EMBEDDING_MODEL": MODEL}):
                with self.subTest(env=env), self.assertRaises(EmbeddingUnavailable):
                    OpenAICompatibleEmbeddingProvider.from_environment(env)
        with self.assertRaises(EmbeddingUnavailable):
            OpenAICompatibleEmbeddingProvider.from_environment(
                {
                    "VOICEVAULT_EMBEDDING_BASE_URL": "file:///private/model",
                    "VOICEVAULT_EMBEDDING_MODEL": MODEL,
                }
            )

    def test_request_body_headers_endpoint_and_key_redaction_are_exact(self) -> None:
        provider, opener = provider_for(response([[0.1, 0.2], [0.3, 0.4]]))
        batch = provider.embed(("first", "second"))
        request, timeout = opener.calls[0]

        self.assertEqual(request.full_url, "http://127.0.0.1:9999/v1/embeddings")
        self.assertEqual(json.loads(request.data), {"model": MODEL, "input": ["first", "second"]})
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {API_KEY}")
        self.assertEqual(timeout, 7.5)
        visible = repr(provider) + repr(batch) + batch.provider_fingerprint
        self.assertNotIn(API_KEY, visible)

        no_key_provider, no_key_opener = provider_for(response([[0.1]]), api_key="")
        no_key_provider.embed(("first",))
        self.assertIsNone(no_key_opener.calls[0][0].get_header("Authorization"))

    def test_reordered_indices_are_restored_and_fingerprint_is_stable(self) -> None:
        payload = response([[3, 4], [1, 2]], indices=[1, 0])
        provider, _ = provider_for(payload)
        batch = provider.embed(("zero", "one"))
        again, _ = provider_for(payload)

        self.assertEqual(batch.vectors, ((1.0, 2.0), (3.0, 4.0)))
        self.assertEqual(batch.dimension, 2)
        self.assertEqual(batch.provider_fingerprint, provider.fingerprint_for_dimension(2))
        self.assertEqual(batch.provider_fingerprint, again.embed(("zero", "one")).provider_fingerprint)
        self.assertRegex(batch.provider_fingerprint, r"^[0-9a-f]{64}$")
        for dimension in (0, -1, True, 1.5):
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                provider.fingerprint_for_dimension(dimension)  # type: ignore[arg-type]

        changed_base = OpenAICompatibleEmbeddingProvider.from_environment(
            {
                "VOICEVAULT_EMBEDDING_BASE_URL": "https://embedding.example/v1",
                "VOICEVAULT_EMBEDDING_MODEL": MODEL,
            },
            opener=RecordingOpener(payload),
        )
        changed_model = OpenAICompatibleEmbeddingProvider.from_environment(
            {
                "VOICEVAULT_EMBEDDING_BASE_URL": BASE_URL,
                "VOICEVAULT_EMBEDDING_MODEL": "embedding-model-b",
            },
            opener=RecordingOpener(response([[1, 2], [3, 4]], model="embedding-model-b")),
        )
        self.assertNotEqual(
            provider.fingerprint_for_dimension(2),
            changed_base.fingerprint_for_dimension(2),
        )
        self.assertNotEqual(
            provider.fingerprint_for_dimension(2),
            changed_model.fingerprint_for_dimension(2),
        )

    def test_count_duplicate_missing_and_noninteger_indices_are_rejected(self) -> None:
        invalid_payloads = (
            response([[1.0]]),
            response([[1.0], [2.0]], indices=[0, 0]),
            response([[1.0], [2.0]], indices=[0, 2]),
            response([[1.0], [2.0]], indices=[0, True]),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                provider, _ = provider_for(payload)
                with self.assertRaises(EmbeddingResponseInvalid):
                    provider.embed(("first", "second"))

    def test_model_and_vector_dimensions_must_match_contract(self) -> None:
        invalid_payloads = (
            response([[1.0]], model="other-model"),
            response([[]]),
            response([[1.0, 2.0], [3.0]]),
        )
        inputs = (("one",), ("one",), ("one", "two"))
        for payload, texts in zip(invalid_payloads, inputs):
            with self.subTest(payload=payload):
                provider, _ = provider_for(payload)
                with self.assertRaises(EmbeddingResponseInvalid):
                    provider.embed(texts)

    def test_bool_nan_inf_and_non_numeric_vector_values_are_rejected(self) -> None:
        for value in (True, float("nan"), float("inf"), float("-inf"), "1.0", None):
            with self.subTest(value=value):
                provider, _ = provider_for(response([[value]]))
                with self.assertRaises(EmbeddingResponseInvalid):
                    provider.embed(("one",))

    def test_invalid_json_http_and_url_errors_are_sanitized_domain_errors(self) -> None:
        invalid_json = RecordingOpener()
        invalid_json.response = None
        provider = OpenAICompatibleEmbeddingProvider.from_environment(
            {
                "VOICEVAULT_EMBEDDING_BASE_URL": BASE_URL,
                "VOICEVAULT_EMBEDDING_MODEL": MODEL,
                "VOICEVAULT_EMBEDDING_API_KEY": API_KEY,
            },
            opener=invalid_json,
        )
        invalid_json.open = lambda request, timeout: FakeResponse(b"not-json", raw=True)  # type: ignore[method-assign]
        with self.assertRaises(EmbeddingResponseInvalid) as malformed:
            provider.embed(("one",))

        errors = (
            HTTPError("https://private.example", 500, f"failed {API_KEY}", {}, io.BytesIO()),
            URLError(f"private path and {API_KEY}"),
            OSError(f"private path and {API_KEY}"),
        )
        for error in errors:
            opener = RecordingOpener(error=error)
            provider = OpenAICompatibleEmbeddingProvider.from_environment(
                {
                    "VOICEVAULT_EMBEDDING_BASE_URL": BASE_URL,
                    "VOICEVAULT_EMBEDDING_MODEL": MODEL,
                    "VOICEVAULT_EMBEDDING_API_KEY": API_KEY,
                },
                opener=opener,
            )
            with self.assertRaises(EmbeddingUnavailable) as raised:
                provider.embed(("one",))
            visible = str(raised.exception) + repr(raised.exception)
            self.assertNotIn(API_KEY, visible)
            self.assertNotIn("private", visible)
        self.assertNotIn(API_KEY, str(malformed.exception))


if __name__ == "__main__":
    unittest.main()
