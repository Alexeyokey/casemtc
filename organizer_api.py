"""Async client for the organizer proxy; no RAG or consultation policy here.

Python >= 3.11. Keep this module at the repository root so the platform
exporter can copy it without changing imports. Wire formats follow the
organizer's sample; they still require a live compatibility check.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
import time
from typing import Any, TypeVar

import httpx


class ConfigurationError(ValueError):
    """Invalid configuration. Messages contain field names, never values."""


class OrganizerAPIError(Exception):
    """Safe error: no response body, URL, request, key or original exception."""

    def __init__(
        self, code: str, operation: str, *, status_code: int | None = None,
        attempts: int = 0,
    ) -> None:
        self.code = code
        self.operation = operation
        self.status_code = status_code
        self.attempts = attempts
        super().__init__(code)

    def __str__(self) -> str:
        return (
            f"{self.operation}: {self.code} "
            f"(status={self.status_code}, attempts={self.attempts})"
        )


class AuthenticationError(OrganizerAPIError):
    """HTTP 401: report organizer access problems, do not change keys/retry."""


def _number(value: Any, name: str, *, integer: bool = False, minimum: float = 0,
            maximum: float | None = None, inclusive: bool = False) -> int | float:
    valid = _finite(value)
    valid = valid and (not integer or isinstance(value, int))
    valid = valid and (value >= minimum if inclusive else value > minimum)
    valid = valid and (maximum is None or value <= maximum)
    if not valid:
        raise ConfigurationError(f"Invalid {name}")
    return value


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class APIConfig:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    embedding_model: str = "baai/bge-m3"
    reranker_model: str = "qwen/qwen3-reranker-8b"
    embedding_key: str | None = field(default=None, repr=False)
    reranker_key: str | None = field(default=None, repr=False)
    temperature: float = 0.0
    max_tokens: int = 2048
    reranker_top_n: int = 2
    max_workers: int = 3
    max_retries: int = 2
    request_timeout_s: float = 30.0
    call_timeout_s: float = 60.0
    run_budget_s: float = 1100.0
    retry_backoff_s: float = 0.5
    max_batch_size: int = 32
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        try:
            url = httpx.URL(self.base_url) if _text(self.base_url) else None
            valid_url = (
                url is not None and url.scheme in ("http", "https") and url.host
                and not url.userinfo and not url.query and not url.fragment
                and "?" not in self.base_url and "#" not in self.base_url
                and (url.port is None or 1 <= url.port <= 65535)
                and not any(ord(c) < 32 or ord(c) == 127 for c in self.base_url)
            )
        except (httpx.InvalidURL, ValueError, TypeError):
            valid_url = False
        if not valid_url:
            raise ConfigurationError("Invalid API base URL")
        for name in ("api_key", "embedding_key", "reranker_key"):
            value = getattr(self, name)
            if value is None and name != "api_key":
                continue
            if not _text(value) or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ConfigurationError(f"Invalid {name}")
        for name in ("model", "embedding_model", "reranker_model"):
            if not _text(getattr(self, name)):
                raise ConfigurationError(f"Missing {name}")
        for name in ("max_tokens", "reranker_top_n", "max_workers",
                     "max_batch_size", "max_response_bytes"):
            _number(getattr(self, name), name, integer=True)
        _number(self.max_workers, "max_workers", maximum=32)
        _number(self.max_retries, "max_retries", integer=True, inclusive=True, maximum=5)
        for name in ("request_timeout_s", "call_timeout_s", "run_budget_s"):
            _number(getattr(self, name), name)
        _number(self.run_budget_s, "run_budget_s", maximum=1200)
        _number(self.retry_backoff_s, "retry_backoff_s", inclusive=True, maximum=30)
        _number(self.temperature, "temperature", inclusive=True, maximum=2)
        object.__setattr__(self, "base_url", str(url).rstrip("/"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> APIConfig:
        env = os.environ if environ is None else environ

        def value(*names: str, default: str = "") -> str:
            for name in names:
                v = env.get(name, "").strip()
                if v and v != name:
                    return v
            return default

        def numeric(name: str, default: int | float, kind: type) -> int | float:
            raw = value(name)
            if not raw:
                return default
            try:
                result = kind(raw)
            except (ValueError, OverflowError):
                result = None
            if result is None:
                raise ConfigurationError(f"Invalid {name}")
            return result

        return cls(
            base_url=value("LLM_BASE_URL", "OPENAI_BASE_URL", "BASE_URL"),
            api_key=value("LLM_API_KEY", "OPENAI_API_KEY"),
            model=value("LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL"),
            embedding_model=value("EMB_MODEL", default="baai/bge-m3"),
            reranker_model=value("RERANKER_MODEL", default="qwen/qwen3-reranker-8b"),
            embedding_key=value("EMBEDDER_API_KEY") or None,
            reranker_key=value("RERANKER_API_KEY") or None,
            temperature=numeric("TEMPERATURE", 0.0, float),
            max_tokens=numeric("MAX_TOKENS", 2048, int),
            reranker_top_n=numeric("RERANKER_TOP_N", 2, int),
            max_workers=numeric("MAX_WORKERS", 3, int),
            max_retries=numeric("API_MAX_RETRIES", 2, int),
            request_timeout_s=numeric("API_REQUEST_TIMEOUT_S", 30.0, float),
            call_timeout_s=numeric("API_CALL_TIMEOUT_S", 60.0, float),
            run_budget_s=numeric("API_RUN_BUDGET_S", 1100.0, float),
            retry_backoff_s=numeric("API_RETRY_BACKOFF_S", 0.5, float),
            max_batch_size=numeric("API_MAX_BATCH_SIZE", 32, int),
            max_response_bytes=numeric("API_MAX_RESPONSE_BYTES", 8 * 1024 * 1024, int),
        )


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ChatResult:
    text: str = field(repr=False)
    finish_reason: str | None
    usage: TokenUsage | None
    reasoning_present: bool = False


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    usage: TokenUsage | None


@dataclass(frozen=True)
class RankedDocument:
    index: int
    score: float | None


@dataclass(frozen=True)
class RerankResult:
    ranks: tuple[RankedDocument, ...]


@dataclass(frozen=True)
class RequestMetric:
    operation: str
    outcome: str
    attempts: int
    elapsed_seconds: float
    status_code: int | None
    error_code: str | None


T = TypeVar("T")


class OrganizerClient:
    """Use one instance per event loop; share it among tasks in that loop.

    The run budget starts at construction and is NOT reset by each request.
    Pass an earlier monotonic deadline to include preprocessing/indexing.
    Close only after the tasks using the client have completed/cancelled.
    """

    def __init__(self, config: APIConfig, *, deadline: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        if deadline is not None:
            _number(deadline, "deadline")
        self._deadline = min(time.monotonic() + config.run_budget_s,
                             deadline if deadline is not None else math.inf)
        self._semaphore = asyncio.Semaphore(config.max_workers)
        self._metrics: deque[RequestMetric] = deque(maxlen=1000)
        self._closed = False
        self._http = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(config.request_timeout_s, connect=min(5, config.request_timeout_s),
                                  pool=min(5, config.request_timeout_s)),
            limits=httpx.Limits(max_connections=config.max_workers,
                                max_keepalive_connections=config.max_workers),
        )

    async def __aenter__(self) -> OrganizerClient:
        if self._closed:
            raise OrganizerAPIError("client_closed", "client")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await self._http.aclose()

    @property
    def remaining_budget_s(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    @property
    def metrics(self) -> tuple[RequestMetric, ...]:
        """Bounded, content-free history, including failures and cancellation."""
        return tuple(self._metrics)

    async def chat(self, messages: Sequence[Mapping[str, str]], *,
                   temperature: float | None = None, max_tokens: int | None = None,
                   extra_body: Mapping[str, Any] | None = None) -> ChatResult:
        operation = "chat"
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence) or not messages:
            raise OrganizerAPIError("invalid_messages", operation)
        copied = []
        for message in messages:
            if (not isinstance(message, Mapping)
                    or message.get("role") not in ("system", "user", "assistant", "developer")
                    or not _text(message.get("content"))):
                raise OrganizerAPIError("invalid_messages", operation)
            copied.append({"role": message["role"], "content": message["content"]})
        temp = self.config.temperature if temperature is None else temperature
        tokens = self.config.max_tokens if max_tokens is None else max_tokens
        try:
            _number(temp, "temperature", inclusive=True, maximum=2)
            _number(tokens, "max_tokens", integer=True)
        except ConfigurationError:
            raise OrganizerAPIError("invalid_generation_parameters", operation) from None
        body = {"model": self.config.model, "messages": copied, "temperature": temp,
                "max_tokens": tokens, "stream": False}
        if extra_body is not None:
            if (not isinstance(extra_body, Mapping)
                    or any(k in body for k in extra_body)
                    or any(not isinstance(k, str) for k in extra_body)):
                raise OrganizerAPIError("invalid_extra_body", operation)
            body.update(extra_body)

        def parse(data: Any) -> ChatResult:
            try:
                choice = data["choices"][0]
                message = choice["message"]
                text = message["content"]
                reason = choice.get("finish_reason")
                if reason == "length":
                    raise OrganizerAPIError("truncated_response", operation)
                if reason not in (None, "stop") or not _text(text):
                    raise ValueError
                return ChatResult(text.strip(), reason, _usage(data, operation),
                                  bool(message.get("reasoning_content")))
            except (KeyError, IndexError, TypeError, AttributeError, ValueError):
                raise OrganizerAPIError("invalid_response", operation) from None

        return await self._execute(operation, "/chat/completions", self.config.api_key, body, parse)

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        operation = "embeddings"
        self._validate_texts(texts, operation)
        texts = tuple(texts)

        def parse(data: Any) -> EmbeddingResult:
            try:
                entries = data["data"]
                if not isinstance(entries, list) or len(entries) != len(texts):
                    raise ValueError
                indexed = ["index" in entry for entry in entries]
                if any(indexed) and not all(indexed):
                    raise ValueError
                vectors: list[tuple[float, ...] | None] = [None] * len(texts)
                dimension = None
                for pos, entry in enumerate(entries):
                    index = entry["index"] if all(indexed) else pos
                    if type(index) is not int or not 0 <= index < len(texts) or vectors[index] is not None:
                        raise ValueError
                    vector = entry["embedding"]
                    if (not isinstance(vector, list) or not vector
                            or any(not _finite(x) for x in vector)):
                        raise ValueError
                    if dimension is not None and len(vector) != dimension:
                        raise ValueError
                    dimension = len(vector)
                    vectors[index] = tuple(float(x) for x in vector)
                return EmbeddingResult(tuple(vectors), _usage(data, operation))
            except (KeyError, TypeError, AttributeError, ValueError):
                raise OrganizerAPIError("invalid_response", operation) from None

        return await self._execute(
            operation, "/embeddings", self.config.embedding_key or self.config.api_key,
            {"model": self.config.embedding_model, "input": list(texts)}, parse,
        )

    async def rerank(self, query: str, documents: Sequence[str], *,
                     top_n: int | None = None) -> RerankResult:
        operation = "rerank"
        self._validate_texts(documents, operation)
        documents = tuple(documents)
        n = min(self.config.reranker_top_n, len(documents)) if top_n is None else top_n
        if not _text(query) or type(n) is not int or not 1 <= n <= len(documents):
            raise OrganizerAPIError("invalid_rerank_parameters", operation)

        def parse(data: Any) -> RerankResult:
            try:
                entries = data.get("results", data.get("data"))
                if not isinstance(entries, list) or not entries:
                    raise ValueError
                ranks = []
                seen = set()
                for entry in entries:
                    index = entry["index"]
                    score = entry.get("relevance_score", entry.get("score"))
                    if (type(index) is not int or not 0 <= index < len(documents)
                            or index in seen or (score is not None and not _finite(score))):
                        raise ValueError
                    seen.add(index)
                    ranks.append(RankedDocument(index, float(score) if score is not None else None))
                return RerankResult(tuple(ranks[:n]))
            except (KeyError, TypeError, AttributeError, ValueError):
                raise OrganizerAPIError("invalid_response", operation) from None

        return await self._execute(
            operation, "/rerank", self.config.reranker_key or self.config.api_key,
            {"model": self.config.reranker_model, "query": query,
             "documents": list(documents), "top_n": n}, parse,
        )

    def _validate_texts(self, texts: Any, operation: str) -> None:
        if (isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence)
                or not 1 <= len(texts) <= self.config.max_batch_size
                or any(not _text(text) for text in texts)):
            raise OrganizerAPIError("invalid_batch", operation)

    async def _execute(self, operation: str, endpoint: str, key: str,
                       body: dict[str, Any], parse: Callable[[Any], T]) -> T:
        # Freeze caller-owned data before the first await; reject NaN and objects.
        invalid_json = False
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            invalid_json = True
        if invalid_json:
            raise OrganizerAPIError("invalid_json_parameters", operation)
        started = time.monotonic()
        deadline = min(self._deadline, started + self.config.call_timeout_s)
        attempts = 0
        status = None
        outcome = "error"
        error_code = None

        def budget_error() -> OrganizerAPIError:
            code = "run_budget_exceeded" if self._deadline <= deadline else "call_deadline_exceeded"
            return OrganizerAPIError(code, operation)

        try:
            if self._closed:
                raise OrganizerAPIError("client_closed", operation)
            if deadline <= time.monotonic():
                raise budget_error()
            async with asyncio.timeout(deadline - time.monotonic()):
                for attempt in range(self.config.max_retries + 1):
                    retry_after = None
                    code = "network_error"
                    async with self._semaphore:
                        if self._closed:
                            raise OrganizerAPIError("client_closed", operation)
                        attempts = attempt + 1
                        status = None
                        try:
                            async with self._http.stream(
                                "POST", self.config.base_url + endpoint, content=encoded,
                                headers={"Authorization": "Bearer " + key,
                                         "Content-Type": "application/json", "Accept": "application/json"},
                            ) as response:
                                status = response.status_code
                                if status == 401:
                                    raise AuthenticationError("authentication_failed", operation, status_code=status)
                                if 200 <= status < 300:
                                    encoding = response.headers.get("Content-Encoding", "").strip().lower()
                                    if encoding not in ("", "identity"):
                                        # HTTPX decodes a compressed chunk before yielding it to the size check.
                                        raise OrganizerAPIError("unsupported_content_encoding", operation,
                                                                status_code=status)
                                    raw = bytearray()
                                    async for chunk in response.aiter_bytes():
                                        if len(raw) + len(chunk) > self.config.max_response_bytes:
                                            raise OrganizerAPIError("response_too_large", operation, status_code=status)
                                        raw.extend(chunk)
                                    invalid_response = False
                                    try:
                                        data = json.loads(raw)
                                    except (ValueError, UnicodeError, RecursionError):
                                        invalid_response = True
                                    if invalid_response:
                                        raise OrganizerAPIError("invalid_response", operation, status_code=status)
                                    result = parse(data)
                                    if time.monotonic() >= deadline:
                                        raise budget_error()
                                    outcome = "success"
                                    return result
                                retryable = status in (408, 429) or 500 <= status < 600
                                code = "rate_limited" if status == 429 else "http_error"
                                if not retryable:
                                    raise OrganizerAPIError(code, operation, status_code=status)
                                retry_after = _retry_after(response.headers.get("Retry-After"))
                        except httpx.TimeoutException:
                            code = "request_timeout"
                        except httpx.RequestError:
                            code = "network_error"
                    if attempt == self.config.max_retries:
                        raise OrganizerAPIError(code, operation, status_code=status)
                    delay = retry_after if retry_after is not None else min(5.0, self.config.retry_backoff_s * 2 ** attempt)
                    if delay >= deadline - time.monotonic():
                        raise budget_error()
                    # Release the worker slot during backoff. Queueing and sleep
                    # remain inside the same absolute call/run deadline.
                    await asyncio.sleep(delay)
        except TimeoutError:
            error_code = budget_error().code
            raise OrganizerAPIError(error_code, operation, status_code=status, attempts=attempts) from None
        except OrganizerAPIError as error:
            error.attempts = attempts
            if error.status_code is None:
                error.status_code = status
            error_code = error.code
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            error_code = "cancelled"
            raise
        finally:
            self._metrics.append(RequestMetric(
                operation, outcome, attempts, time.monotonic() - started, status, error_code,
            ))


def _finite(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _usage(data: Mapping[str, Any], operation: str) -> TokenUsage | None:
    usage = data.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise OrganizerAPIError("invalid_response", operation)
    counts = [usage.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(x is not None and (type(x) is not int or x < 0) for x in counts):
        raise OrganizerAPIError("invalid_response", operation)
    return TokenUsage(*counts)


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is not None:
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    return None
