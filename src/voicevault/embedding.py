from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


TEXT_NORMALIZATION_VERSION = "voicevault-text-v1"
CHUNK_RULE_VERSION = "paragraph-window-v1"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class EmbeddingError(Exception):
    """Base class for stable, sanitized embedding failures."""


class EmbeddingUnavailable(EmbeddingError):
    """Embedding configuration or transport is unavailable."""


class EmbeddingResponseInvalid(EmbeddingError):
    """The provider response violates the embedding contract."""


class EmbeddingProvider(Protocol):
    def fingerprint_for_dimension(self, dimension: int) -> str:
        """Return current identity, using dimension as a caller-known fallback."""
        ...

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        ...


@dataclass(frozen=True)
class EmbeddingBatch:
    model: str
    dimension: int
    provider_fingerprint: str
    vectors: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Embedding batch model is required.")
        if (
            not isinstance(self.dimension, int)
            or isinstance(self.dimension, bool)
            or self.dimension < 1
        ):
            raise ValueError("Embedding batch dimension must be positive.")
        if (
            not isinstance(self.provider_fingerprint, str)
            or _SHA256_PATTERN.fullmatch(self.provider_fingerprint) is None
        ):
            raise ValueError("Embedding provider fingerprint must be a SHA-256 digest.")
        try:
            vectors = tuple(
                tuple(_finite_float(value) for value in vector)
                for vector in self.vectors
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Embedding batch vectors must contain finite numbers.") from error
        if not vectors or any(len(vector) != self.dimension for vector in vectors):
            raise ValueError("Embedding batch vectors must have one positive dimension.")
        object.__setattr__(self, "vectors", vectors)


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        opener: Any,
        timeout: float,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self._api_key = api_key
        self._opener = opener
        self.timeout = timeout

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        opener: Any = None,
        timeout: float = 30.0,
    ) -> OpenAICompatibleEmbeddingProvider:
        source = os.environ if env is None else env
        try:
            raw_base_url = source.get("VOICEVAULT_EMBEDDING_BASE_URL", "")
            raw_model = source.get("VOICEVAULT_EMBEDDING_MODEL", "")
            raw_api_key = source.get("VOICEVAULT_EMBEDDING_API_KEY", "")
        except (AttributeError, TypeError):
            raise EmbeddingUnavailable("Embedding configuration is unavailable.") from None
        if not all(isinstance(value, str) for value in (raw_base_url, raw_model, raw_api_key)):
            raise EmbeddingUnavailable("Embedding configuration is unavailable.")
        base_url = _normalize_base_url(raw_base_url)
        model = raw_model.strip()
        if not model:
            raise EmbeddingUnavailable("Embedding configuration is unavailable.")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError("Embedding timeout must be positive and finite.")
        api_key = raw_api_key.strip() or None
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            opener=opener,
            timeout=float(timeout),
        )

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleEmbeddingProvider(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout={self.timeout!r})"
        )

    def fingerprint_for_dimension(self, dimension: int) -> str:
        """Use the caller-known dimension because the remote API does not advertise it."""
        validated_dimension = _require_dimension(dimension)
        return _provider_fingerprint(
            provider_type="openai-compatible",
            model=self.model,
            dimension=validated_dimension,
            base_url=self.base_url,
        )

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        validated_texts = _validate_texts(texts)
        body = json.dumps(
            {"model": self.model, "input": list(validated_texts)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            request = Request(
                f"{self.base_url}/embeddings",
                data=body,
                headers=headers,
                method="POST",
            )
            with self._open(request) as response:
                raw = response.read()
        except EmbeddingError:
            raise
        except (HTTPError, URLError, OSError, TypeError, ValueError):
            raise EmbeddingUnavailable("Embedding provider is unavailable.") from None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.") from None
        vectors = _validate_response(
            payload,
            expected_model=self.model,
            expected_count=len(validated_texts),
        )
        dimension = len(vectors[0])
        try:
            return EmbeddingBatch(
                model=self.model,
                dimension=dimension,
                provider_fingerprint=self.fingerprint_for_dimension(dimension),
                vectors=vectors,
            )
        except ValueError:
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.") from None

    def _open(self, request: Request):
        if self._opener is None:
            return urlopen(request, timeout=self.timeout)
        if hasattr(self._opener, "open"):
            return self._opener.open(request, timeout=self.timeout)
        return self._opener(request, timeout=self.timeout)


class FakeEmbeddingProvider:
    def __init__(
        self,
        *,
        model: str = "fake-embedding-v1",
        dimension: int = 8,
        fail: bool = False,
    ) -> None:
        self._model = _require_model(model)
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension < 1
        ):
            raise ValueError("Fake embedding dimension must be positive.")
        self.dimension = dimension
        self._fail = bool(fail)
        self._batches: list[tuple[str, ...]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def batches(self) -> tuple[tuple[str, ...], ...]:
        return tuple(self._batches)

    def set_model(self, model: str) -> None:
        self._model = _require_model(model)

    def set_failure(self, enabled: bool) -> None:
        self._fail = bool(enabled)

    def fingerprint_for_dimension(self, dimension: int) -> str:
        """Return identity for this deterministic provider's configured dimension."""
        _require_dimension(dimension)
        return _provider_fingerprint(
            provider_type="fake",
            model=self.model,
            dimension=self.dimension,
        )

    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        validated_texts = _validate_texts(texts)
        self._batches.append(validated_texts)
        if self._fail:
            raise EmbeddingUnavailable("Fake embedding provider is unavailable.")
        vectors = tuple(
            _fake_vector(text, model=self.model, dimension=self.dimension)
            for text in validated_texts
        )
        return EmbeddingBatch(
            model=self.model,
            dimension=self.dimension,
            provider_fingerprint=self.fingerprint_for_dimension(self.dimension),
            vectors=vectors,
        )


def _validate_texts(texts: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(texts, tuple) or not texts:
        raise ValueError("Embedding input must be a non-empty text tuple.")
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding input texts must be non-empty strings.")
    return texts


def _validate_response(
    payload: Any,
    *,
    expected_model: str,
    expected_count: int,
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(payload, dict) or payload.get("model") != expected_model:
        raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
    indexed: dict[int, tuple[float, ...]] = {}
    dimension: int | None = None
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
        index = item.get("index")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= expected_count
            or index in indexed
        ):
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
        raw_vector = item.get("embedding")
        if not isinstance(raw_vector, list) or not raw_vector:
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
        try:
            vector = tuple(_finite_float(value) for value in raw_vector)
        except (TypeError, ValueError):
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.") from None
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
        indexed[index] = vector
    if set(indexed) != set(range(expected_count)):
        raise EmbeddingResponseInvalid("Embedding provider returned an invalid response.")
    return tuple(indexed[index] for index in range(expected_count))


def _finite_float(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("Embedding value must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("Embedding value must be finite.")
    return normalized


def _normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingUnavailable("Embedding configuration is unavailable.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise EmbeddingUnavailable("Embedding configuration is unavailable.") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EmbeddingUnavailable("Embedding configuration is unavailable.")
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    normalized = urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def _provider_fingerprint(
    *,
    provider_type: str,
    model: str,
    dimension: int,
    base_url: str | None = None,
) -> str:
    identity: dict[str, Any] = {
        "chunk_rule_version": CHUNK_RULE_VERSION,
        "dimension": dimension,
        "model": model,
        "provider_type": provider_type,
        "text_normalization_version": TEXT_NORMALIZATION_VERSION,
    }
    if base_url is not None:
        identity["base_url"] = base_url
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _fake_vector(text: str, *, model: str, dimension: int) -> tuple[float, ...]:
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(
            f"{model}\0{text}\0{counter}".encode("utf-8")
        ).digest()
        for offset in range(0, len(digest), 4):
            raw = int.from_bytes(digest[offset : offset + 4], "big")
            values.append((raw / 0xFFFFFFFF) * 2.0 - 1.0)
            if len(values) == dimension:
                break
        counter += 1
    return tuple(values)


def _require_model(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Embedding model is required.")
    return value.strip()


def _require_dimension(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("Embedding dimension must be positive.")
    return value
