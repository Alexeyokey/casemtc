"""Synthetic contract/fault tests. Never access organizer credentials or KB."""

import asyncio
from collections import deque
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import subprocess
import sys
import threading
import time
import traceback
import unittest
from unittest.mock import patch

import httpx

from organizer_api import APIConfig, AuthenticationError, ConfigurationError, OrganizerAPIError, OrganizerClient


KEY = "synthetic-test-key-not-a-real-secret"
PRIVATE = "synthetic-private-client-text"
MESSAGES = [{"role": "user", "content": "Нужна консультация\nпо учёту."}]


def config(**kwargs):
    defaults = dict(base_url="http://127.0.0.1:1/prefix/v1", api_key=KEY,
                    model="synthetic-generator", retry_backoff_s=0, run_budget_s=5)
    defaults.update(kwargs)
    return APIConfig(**defaults)


def answer(text="Проверочный ответ", **kwargs):
    return {"choices": [{"message": {"content": text, "role": "assistant"},
                         "finish_reason": "stop"}], **kwargs}


class ConfigTests(unittest.TestCase):
    def test_aliases_and_precedence(self):
        cases = [
            ({"LLM_BASE_URL": "http://first/v1", "OPENAI_BASE_URL": "http://second/v1"}, "http://first/v1"),
            ({"LLM_BASE_URL": "  ", "OPENAI_BASE_URL": "http://second/v1"}, "http://second/v1"),
            ({"LLM_BASE_URL": "LLM_BASE_URL", "BASE_URL": "http://legacy/api/"}, "http://legacy/api"),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                result = APIConfig.from_env({**env, "OPENAI_API_KEY": KEY, "GEN_MODEL": "gen"})
                self.assertEqual(result.base_url, expected)
                self.assertEqual(result.model, "gen")
        result = APIConfig.from_env({"LLM_BASE_URL": "http://proxy", "LLM_API_KEY": KEY,
                                     "OPENAI_API_KEY": "other", "LLM_MODEL": "first",
                                     "OPENAI_MODEL": "second", "GEN_MODEL": "third"})
        self.assertEqual(result.api_key, KEY)
        self.assertEqual(result.model, "first")

    def test_openai_model_precedes_legacy_gen_model(self):
        result = APIConfig.from_env({"OPENAI_BASE_URL": "http://proxy", "OPENAI_API_KEY": KEY,
                                     "OPENAI_MODEL": "second", "GEN_MODEL": "third"})
        self.assertEqual(result.model, "second")

    def test_generation_model_has_no_hardcoded_fallback(self):
        for missing in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
            env = {"LLM_BASE_URL": "http://proxy", "LLM_API_KEY": KEY, "LLM_MODEL": "generator"}
            del env[missing]
            with self.subTest(missing=missing), self.assertRaises(ConfigurationError):
                APIConfig.from_env(env)

    def test_urls_reject_credentials_query_and_fragment_without_echo(self):
        for url in ("", "ftp://proxy", "http://", "http://user:"+KEY+"@proxy/v1",
                    "http://proxy?token="+KEY, "http://proxy#"+KEY, "http://proxy:999999"):
            with self.subTest(url=url), self.assertRaises(ConfigurationError) as error:
                config(base_url=url)
            self.assertNotIn(KEY, str(error.exception))

    def test_values_are_stripped_and_optional_keys_fall_back(self):
        env = {"LLM_BASE_URL": " http://proxy/v1/ ", "LLM_API_KEY": " "+KEY+" ",
               "LLM_MODEL": " generator ", "EMBEDDER_API_KEY": " "}
        result = APIConfig.from_env(env)
        self.assertEqual(result.base_url, "http://proxy/v1")
        self.assertEqual(result.api_key, KEY)
        self.assertIsNone(result.embedding_key)

    def test_invalid_numeric_settings_fail_explicitly(self):
        env = {"LLM_BASE_URL": "http://proxy", "LLM_API_KEY": KEY, "LLM_MODEL": "generator"}
        for name, value in (("MAX_WORKERS", "0"), ("MAX_WORKERS", "999"),
                            ("MAX_TOKENS", "1.5"), ("MAX_TOKENS", "bad-"+KEY),
                            ("TEMPERATURE", "nan"), ("TEMPERATURE", "inf"),
                            ("TEMPERATURE", "2.1"), ("API_MAX_RETRIES", "-1"),
                            ("API_MAX_RETRIES", "6"), ("API_RUN_BUDGET_S", "1201"),
                            ("API_CALL_TIMEOUT_S", "0"), ("API_MAX_BATCH_SIZE", "-1")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigurationError) as error:
                APIConfig.from_env({**env, name: value})
            self.assertNotIn(KEY, str(error.exception))

    def test_direct_construction_validates_types_and_nonfinite_numbers(self):
        for field, value in (("max_workers", True), ("max_tokens", 1.0),
                             ("request_timeout_s", float("inf")),
                             ("run_budget_s", float("nan")), ("max_tokens", 10**1000)):
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                config(**{field: value})

    def test_config_repr_does_not_contain_keys_or_base_url(self):
        result = config(embedding_key=KEY+"-emb", reranker_key=KEY+"-rank")
        self.assertNotIn(KEY, repr(result))
        self.assertNotIn("127.0.0.1", repr(result))

    def test_invalid_header_key_is_rejected(self):
        for value in ("", "hello world", "header\r\ninjection", "кириллица"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                config(api_key=value)

    def test_numeric_and_model_settings_from_environment(self):
        result = APIConfig.from_env({"LLM_BASE_URL": "http://proxy", "LLM_API_KEY": KEY,
                                    "LLM_MODEL": "new-model", "TEMPERATURE": "0.2",
                                    "MAX_TOKENS": "500", "MAX_WORKERS": "2", "EMB_MODEL": "emb",
                                    "RERANKER_MODEL": "rank", "RERANKER_TOP_N": "1",
                                    "EMBEDDER_API_KEY": "emb-key", "RERANKER_API_KEY": "rank-key"})
        self.assertEqual((result.temperature, result.max_tokens, result.max_workers), (0.2, 500, 2))
        self.assertEqual((result.embedding_model, result.reranker_model), ("emb", "rank"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_wire_format_and_separate_reasoning(self):
        requests = []

        def handle(request):
            requests.append(request)
            result = answer("  **Ответ**\n\nСледующая строка.  ", usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7})
            result["choices"][0]["message"]["reasoning_content"] = PRIVATE
            return httpx.Response(200, json=result)

        async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
            result = await client.chat(MESSAGES, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            self.assertEqual(result.text, "**Ответ**\n\nСледующая строка.")
            self.assertTrue(result.reasoning_present)
            self.assertNotIn(PRIVATE, repr(result))
            self.assertEqual(result.usage.total_tokens, 7)
            self.assertEqual(client.metrics[-1].outcome, "success")
        request = requests[0]
        self.assertEqual(str(request.url), "http://127.0.0.1:1/prefix/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer "+KEY)
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        body = json.loads(request.content)
        self.assertEqual(body["messages"], MESSAGES)
        self.assertEqual(body["model"], "synthetic-generator")
        self.assertIs(body["stream"], False)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    async def test_model_changes_via_configuration(self):
        seen = []

        def handle(request):
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=answer())

        for model in ("model-a", "model-b"):
            async with OrganizerClient(config(model=model), transport=httpx.MockTransport(handle)) as client:
                await client.chat(MESSAGES)
        self.assertEqual(seen, ["model-a", "model-b"])

    async def test_rejects_invalid_inputs_before_network(self):
        def fail(request):
            self.fail("No HTTP request expected")

        async with OrganizerClient(config(), transport=httpx.MockTransport(fail)) as client:
            for messages in ([], "hello", [{"role": "user", "content": " "}], [{"role": "invalid", "content": "x"}]):
                with self.subTest(messages=messages), self.assertRaises(OrganizerAPIError):
                    await client.chat(messages)
            for extra in ({"model": "overwrite"}, {"stream": True}, {"temperature": 1}, {1: "bad"}, {"bad": object()}, {"bad": float("nan")}):
                with self.subTest(extra=extra), self.assertRaises(OrganizerAPIError):
                    await client.chat(MESSAGES, extra_body=extra)
            for kwargs in ({"max_tokens": 0}, {"max_tokens": True}, {"temperature": float("nan")}):
                with self.subTest(kwargs=kwargs), self.assertRaises(OrganizerAPIError):
                    await client.chat(MESSAGES, **kwargs)

    async def test_chat_rejects_malformed_empty_truncated_and_filtered_answers(self):
        cases = [({}, "invalid_response"), ({"choices": []}, "invalid_response"),
                 (answer("  "), "invalid_response"), (answer(None), "invalid_response"),
                 ({"choices": [{"message": {"reasoning_content": PRIVATE}}]}, "invalid_response"),
                 ({"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}, "truncated_response"),
                 ({"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}, "invalid_response"),
                 (answer(usage={"total_tokens": -1}), "invalid_response")]
        for payload, code in cases:
            with self.subTest(payload=payload):
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
                    with self.assertRaises(OrganizerAPIError) as error:
                        await client.chat(MESSAGES)
                    self.assertEqual(error.exception.code, code)
                    self.assertEqual(client.metrics[-1].outcome, "error")

    async def test_embeddings_restore_input_order_without_normalizing(self):
        captured = []

        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"index": 1, "embedding": [3, 4]}, {"index": 0, "embedding": [1, 2]}]})

        async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
            result = await client.embed(["первый", "второй"])
            self.assertEqual(result.vectors, ((1.0, 2.0), (3.0, 4.0)))
        self.assertEqual(captured[0], {"model": "baai/bge-m3", "input": ["первый", "второй"]})

    async def test_unindexed_embeddings_follow_sample_order(self):
        async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"embedding": [1]}, {"embedding": [2]}]}))) as client:
            self.assertEqual((await client.embed(["a", "b"])).vectors, ((1.0,), (2.0,)))

    async def test_invalid_embedding_alignment_or_values_are_rejected(self):
        cases = [[], [{"index": 0, "embedding": [1]}],
                 [{"index": 0, "embedding": [1]}, {"index": 0, "embedding": [2]}],
                 [{"index": -1, "embedding": [1]}, {"index": 1, "embedding": [2]}],
                 [{"index": 0, "embedding": []}, {"index": 1, "embedding": [2]}],
                 [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [2]}],
                 [{"index": 0, "embedding": ["1"]}, {"index": 1, "embedding": [2]}],
                 [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [2, 3]}],
                 [{"index": 0, "embedding": [1]}, {"embedding": [2]}],
                 [{"index": True, "embedding": [1]}, {"index": 1, "embedding": [2]}]]
        for entries in cases:
            with self.subTest(entries=entries):
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": entries}))) as client:
                    with self.assertRaises(OrganizerAPIError):
                        await client.embed(["a", "b"])

    async def test_nonfinite_and_extreme_embedding_numbers_are_rejected(self):
        for literal in ("NaN", "Infinity", "1"+"0"*500):
            with self.subTest(literal=literal[:10]):
                raw = ('{"data":[{"index":0,"embedding":['+literal+']}]}' ).encode()
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw))) as client:
                    with self.assertRaises(OrganizerAPIError):
                        await client.embed(["a"])

    async def test_rerank_formats_and_document_indices(self):
        for wrapper, score_name in (("results", "relevance_score"), ("data", "score")):
            with self.subTest(wrapper=wrapper):
                seen = []

                def handle(request):
                    seen.append(json.loads(request.content))
                    return httpx.Response(200, json={wrapper: [{"index": 1, score_name: 0.8}, {"index": 0, score_name: -1}]})

                async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
                    result = await client.rerank("запрос", ["a", "b"], top_n=1)
                    self.assertEqual(result.ranks[0].index, 1)
                    self.assertEqual(result.ranks[0].score, 0.8)
                    self.assertEqual(len(result.ranks), 1)
                self.assertEqual(seen[0]["top_n"], 1)
                self.assertEqual(seen[0]["documents"], ["a", "b"])

    async def test_invalid_rerank_outputs_and_inputs(self):
        for entries in ([], [{"index": 9, "score": 1}], [{"index": -1, "score": 1}],
                        [{"index": 0, "score": True}],
                        [{"index": 0, "score": 1}, {"index": 0, "score": 2}]):
            with self.subTest(entries=entries):
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": entries}))) as client:
                    with self.assertRaises(OrganizerAPIError):
                        await client.rerank("query", ["a", "b"])
        async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: self.fail("Unexpected network"))) as client:
            for kwargs in ({"top_n": 0}, {"top_n": 3}, {"top_n": True}):
                with self.subTest(kwargs=kwargs), self.assertRaises(OrganizerAPIError):
                    await client.rerank("query", ["a", "b"], **kwargs)

    async def test_rerank_can_return_only_indices_as_in_organizer_sample(self):
        async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"index": 1}, {"index": 0}]}))) as client:
            result = await client.rerank("query", ["a", "b"])
            self.assertEqual([x.index for x in result.ranks], [1, 0])
            self.assertIsNone(result.ranks[0].score)

    async def test_batches_are_bounded_before_network(self):
        async with OrganizerClient(config(max_batch_size=2), transport=httpx.MockTransport(lambda r: self.fail("Unexpected network"))) as client:
            for texts in ([], "a", [""], ["a", "b", "c"]):
                with self.subTest(texts=texts), self.assertRaises(OrganizerAPIError):
                    await client.embed(texts)
                with self.subTest(documents=texts), self.assertRaises(OrganizerAPIError):
                    await client.rerank("query", texts)

    async def test_endpoint_keys_use_explicit_overrides_and_shared_fallback(self):
        for overrides in ({}, {"embedding_key": "embed-only", "reranker_key": "rank-only"}):
            captured = {}

            def handle(request):
                route = request.url.path.rsplit("/", 1)[-1]
                captured[route] = request.headers["Authorization"]
                payload = {"completions": answer(), "embeddings": {"data": [{"index": 0, "embedding": [1]}]},
                           "rerank": {"results": [{"index": 0, "score": 1}]}}[route]
                return httpx.Response(200, json=payload)

            async with OrganizerClient(config(**overrides), transport=httpx.MockTransport(handle)) as client:
                await client.chat(MESSAGES)
                await client.embed(["a"])
                await client.rerank("q", ["a"])
            self.assertEqual(captured["completions"], "Bearer "+KEY)
            self.assertEqual(captured["embeddings"], "Bearer "+overrides.get("embedding_key", KEY))
            self.assertEqual(captured["rerank"], "Bearer "+overrides.get("reranker_key", KEY))

    async def test_authentication_is_distinct_and_not_retried(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(401, json={"error": "token_not_found_in_db "+KEY+PRIVATE})

        async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(AuthenticationError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(len(calls), 1)
            self.assertEqual(error.exception.status_code, 401)
            self.assertEqual(error.exception.attempts, 1)

    async def test_permanent_statuses_and_redirects_are_not_retried(self):
        for status in (301, 302, 307, 400, 403, 404, 422):
            with self.subTest(status=status):
                calls = []

                def handle(request):
                    calls.append(request)
                    return httpx.Response(status, headers={"Location": "https://external.invalid/"+KEY}, content=KEY.encode())

                async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(OrganizerAPIError) as error:
                        await client.chat(MESSAGES)
                    self.assertEqual(error.exception.status_code, status)
                    self.assertEqual(len(calls), 1)

    async def test_retryable_statuses_then_success(self):
        for initial in (408, 429, 500, 503):
            with self.subTest(status=initial):
                responses = deque([initial, initial, 200])
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(responses.popleft(), json=answer()))) as client:
                    result = await client.chat(MESSAGES)
                    self.assertTrue(result.text)
                    self.assertEqual(client.metrics[-1].attempts, 3)
                    self.assertFalse(responses)

    async def test_exhaustion_stops_at_configured_attempt_count(self):
        for status, expected in ((429, "rate_limited"), (503, "http_error")):
            with self.subTest(status=status):
                async with OrganizerClient(config(max_retries=1), transport=httpx.MockTransport(lambda r: httpx.Response(status, content=KEY.encode()))) as client:
                    with self.assertRaises(OrganizerAPIError) as error:
                        await client.chat(MESSAGES)
                    self.assertEqual(error.exception.code, expected)
                    self.assertEqual(error.exception.attempts, 2)

    async def test_transport_errors_and_timeouts_are_sanitized_and_bounded(self):
        for error_type, code in ((httpx.ConnectError, "network_error"), (httpx.ReadTimeout, "request_timeout")):
            with self.subTest(error_type=error_type):
                def handle(request):
                    raise error_type(KEY+PRIVATE, request=request)

                async with OrganizerClient(config(max_retries=1), transport=httpx.MockTransport(handle)) as client:
                    try:
                        await client.chat(MESSAGES)
                    except OrganizerAPIError as error:
                        formatted = traceback.format_exc()
                        self.assertEqual(error.code, code)
                        self.assertEqual(error.attempts, 2)
                        self.assertNotIn(KEY, formatted)
                        self.assertNotIn(PRIVATE, formatted)
                        self.assertIsNone(error.__context__)
                    else:
                        self.fail("Expected safe API error")

    async def test_error_body_and_request_are_absent_from_debug_and_output(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("httpx")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with redirect_stdout(output), redirect_stderr(output):
                async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(401, content=(KEY+PRIVATE).encode()))) as client:
                    try:
                        await client.chat([{"role": "user", "content": PRIVATE}])
                    except OrganizerAPIError:
                        output.write(traceback.format_exc())
                    output.write(repr(client.metrics))
                    output.write(repr(client.config))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertNotIn(KEY, output.getvalue())
        self.assertNotIn(PRIVATE, output.getvalue())

    async def test_invalid_json_does_not_retain_raw_decode_exception(self):
        async with OrganizerClient(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=(KEY+PRIVATE).encode()))) as client:
            try:
                await client.chat(MESSAGES)
            except OrganizerAPIError as error:
                self.assertEqual(error.code, "invalid_response")
                self.assertIsNone(error.__context__)
                self.assertNotIn(KEY, traceback.format_exc())
            else:
                self.fail("Expected JSON failure")

    async def test_deeply_nested_request_is_rejected_before_network(self):
        nested = PRIVATE
        for _ in range(10_000):
            nested = [nested]

        def fail(_):
            self.fail("Invalid request reached the transport")

        async with OrganizerClient(config(), transport=httpx.MockTransport(fail)) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES, extra_body={"nested": nested})
            self.assertEqual(error.exception.code, "invalid_json_parameters")
            self.assertIsNone(error.exception.__context__)

    async def test_deeply_nested_response_is_reported_as_invalid_json(self):
        content = b"[" * 10_000 + b"]" * 10_000
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=content))
        async with OrganizerClient(config(), transport=transport) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "invalid_response")
            self.assertIsNone(error.exception.__context__)
            self.assertEqual(client.metrics[-1].error_code, "invalid_response")

    async def test_body_size_is_bounded(self):
        async with OrganizerClient(config(max_response_bytes=16), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"*17))) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "response_too_large")
            self.assertEqual(error.exception.attempts, 1)

    async def test_unsolicited_gzip_is_rejected_without_reading_the_body(self):
        compressed = gzip.compress(b"x" * 65_536)
        limit = 256
        self.assertLess(len(compressed), limit)
        response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(compressed))
        async with OrganizerClient(config(max_response_bytes=limit),
                                   transport=httpx.MockTransport(lambda _: response)) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "unsupported_content_encoding")
            self.assertEqual(error.exception.attempts, 1)
            self.assertEqual(client.metrics[-1].error_code, "unsupported_content_encoding")
        self.assertFalse(response.is_stream_consumed)
        self.assertTrue(response.is_closed)

    async def test_absolute_call_deadline_cancels_active_request(self):
        cancelled = asyncio.Event()

        async def handle(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        async with OrganizerClient(config(call_timeout_s=0.04), transport=httpx.MockTransport(handle)) as client:
            started = time.monotonic()
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "call_deadline_exceeded")
            self.assertLess(time.monotonic()-started, 1)
            self.assertTrue(cancelled.is_set())

    async def test_shared_run_deadline_includes_queued_requests(self):
        async def handle(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=answer())

        async with OrganizerClient(config(max_workers=1, run_budget_s=0.05, call_timeout_s=2), transport=httpx.MockTransport(handle)) as client:
            results = await asyncio.gather(client.chat(MESSAGES), client.chat(MESSAGES), return_exceptions=True)
            self.assertTrue(all(isinstance(x, OrganizerAPIError) and x.code == "run_budget_exceeded" for x in results))
            self.assertEqual(sorted(m.attempts for m in client.metrics), [0, 1])
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.attempts, 0)

    async def test_external_deadline_starts_before_client_construction(self):
        async with OrganizerClient(config(), deadline=time.monotonic()-1, transport=httpx.MockTransport(lambda r: self.fail("No budget"))) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "run_budget_exceeded")

    async def test_concurrency_is_bounded_for_shared_client(self):
        active = maximum = 0

        async def handle(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.015)
                return httpx.Response(200, json=answer())
            finally:
                active -= 1

        async with OrganizerClient(config(max_workers=2), transport=httpx.MockTransport(handle)) as client:
            await asyncio.gather(*(client.chat(MESSAGES) for _ in range(8)))
        self.assertEqual(maximum, 2)

    async def test_retry_after_is_honored_and_does_not_hold_worker_slot(self):
        first_failed = asyncio.Event()
        order = []

        def handle(request):
            text = json.loads(request.content)["messages"][0]["content"]
            order.append(text)
            if text == "first" and order.count(text) == 1:
                first_failed.set()
                return httpx.Response(429, headers={"Retry-After": "0.04"})
            return httpx.Response(200, json=answer())

        async with OrganizerClient(config(max_workers=1), transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(client.chat([{"role": "user", "content": "first"}]))
            await first_failed.wait()
            await client.chat([{"role": "user", "content": "second"}])
            await task
        self.assertEqual(order, ["first", "second", "first"])

    async def test_retry_after_exceeding_budget_fails_without_early_retry(self):
        for retry_after in ("3600", format_datetime(datetime.now(timezone.utc)+timedelta(hours=1))):
            with self.subTest(retry_after=retry_after):
                async with OrganizerClient(config(call_timeout_s=0.1), transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": retry_after}))) as client:
                    with self.assertRaises(OrganizerAPIError) as error:
                        await client.chat(MESSAGES)
                    self.assertEqual(error.exception.code, "call_deadline_exceeded")
                    self.assertEqual(error.exception.attempts, 1)

    async def test_invalid_retry_after_uses_bounded_fallback(self):
        for value in ("NaN", "Infinity", "-5", "invalid-"+KEY):
            with self.subTest(value=value):
                async with OrganizerClient(config(max_retries=0), transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": value}))) as client:
                    with self.assertRaises(OrganizerAPIError) as error:
                        await client.chat(MESSAGES)
                    self.assertEqual(error.exception.code, "rate_limited")

    async def test_cancellation_propagates_and_releases_capacity(self):
        entered = asyncio.Event()
        calls = 0

        async def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.sleep(10)
            return httpx.Response(200, json=answer())

        async with OrganizerClient(config(max_workers=1), transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(client.chat(MESSAGES))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.metrics[-1].outcome, "cancelled")
            self.assertTrue((await client.chat(MESSAGES)).text)

    async def test_closed_client_rejects_without_network(self):
        client = OrganizerClient(config(), transport=httpx.MockTransport(lambda r: self.fail("Closed")))
        await client.aclose()
        with self.assertRaises(OrganizerAPIError) as error:
            await client.chat(MESSAGES)
        self.assertEqual(error.exception.code, "client_closed")

    async def test_mutating_input_while_queued_cannot_misalign_embeddings(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handle(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1]}]})

        async with OrganizerClient(config(), transport=httpx.MockTransport(handle)) as client:
            texts = ["original"]
            task = asyncio.create_task(client.embed(texts))
            await entered.wait()
            texts.append("late mutation")
            release.set()
            self.assertEqual((await task).vectors, ((1.0,),))


class LocalHTTPTests(unittest.IsolatedAsyncioTestCase):
    """Real TCP/HTTP tests complement MockTransport's synthetic fault cases."""

    @classmethod
    def setUpClass(cls):
        cls.actions = deque()
        cls.requests = []
        cls.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    # Expected when a deadline test closes the TCP connection.
                    pass

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                with cls.lock:
                    cls.requests.append((self.path, dict(self.headers), json.loads(raw)))
                    action = cls.actions.popleft() if cls.actions else {}
                time.sleep(action.get("delay", 0))
                data = action.get("data", answer())
                encoded = json.dumps(data, ensure_ascii=False).encode()
                try:
                    self.send_response(action.get("status", 200))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    if action.get("drip"):
                        for i in range(0, len(encoded), 3):
                            self.wfile.write(encoded[i:i+3])
                            self.wfile.flush()
                            time.sleep(action["drip"])
                    else:
                        self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/organizer/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    async def asyncSetUp(self):
        self.actions.clear()
        self.requests.clear()

    async def test_all_three_routes_over_real_http(self):
        self.actions.extend([{}, {"data": {"data": [{"index": 0, "embedding": [1, 2]}]}},
                             {"data": {"results": [{"index": 0, "relevance_score": 0.9}]}}])
        async with OrganizerClient(config(base_url=self.base_url)) as client:
            self.assertTrue((await client.chat(MESSAGES)).text)
            self.assertEqual((await client.embed(["текст"])).vectors, ((1.0, 2.0),))
            self.assertEqual((await client.rerank("запрос", ["документ"])).ranks[0].index, 0)
        self.assertEqual([x[0] for x in self.requests], ["/organizer/v1/chat/completions", "/organizer/v1/embeddings", "/organizer/v1/rerank"])
        self.assertTrue(all(x[1]["Authorization"] == "Bearer "+KEY for x in self.requests))

    async def test_real_503_then_success(self):
        self.actions.extend([{"status": 503, "data": {"error": PRIVATE}}, {}])
        async with OrganizerClient(config(base_url=self.base_url)) as client:
            await client.chat(MESSAGES)
            self.assertEqual(client.metrics[-1].attempts, 2)
        self.assertEqual(len(self.requests), 2)

    async def test_real_read_timeout(self):
        self.actions.append({"delay": 0.2})
        async with OrganizerClient(config(base_url=self.base_url, request_timeout_s=0.03, max_retries=0)) as client:
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "request_timeout")

    async def test_dripping_response_cannot_bypass_absolute_deadline(self):
        self.actions.append({"data": answer("длинный ответ "*50), "drip": 0.01})
        async with OrganizerClient(config(base_url=self.base_url, request_timeout_s=0.1, call_timeout_s=0.07)) as client:
            started = time.monotonic()
            with self.assertRaises(OrganizerAPIError) as error:
                await client.chat(MESSAGES)
            self.assertEqual(error.exception.code, "call_deadline_exceeded")
            self.assertLess(time.monotonic()-started, 1)

    async def test_external_proxy_environment_is_not_used_for_local_organizer(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            async with OrganizerClient(config(base_url=self.base_url)) as client:
                self.assertTrue((await client.chat(MESSAGES)).text)

    async def test_prepared_live_checker_against_local_fixture_only(self):
        self.actions.extend([{}, {"data": {"data": [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [2]}]}},
                             {"data": {"results": [{"index": 0}, {"index": 1}]}}])
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "examples.i1_live_check", "--live"],
                                         env={"LLM_BASE_URL": self.base_url, "LLM_API_KEY": KEY,
                                              "LLM_MODEL": "synthetic-generator", "MAX_TOKENS": "512"},
                                         capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.requests[0][2]["max_tokens"], 512)
        self.assertNotIn(KEY, result.stdout+result.stderr)


class CommandTests(unittest.TestCase):
    def test_offline_example_needs_no_environment_credentials(self):
        result = subprocess.run([sys.executable, "-m", "examples.i1_offline"], env={},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["real_proxy_checked"], False)
        self.assertEqual(len(payload["metrics"]), 3)

    def test_live_check_without_flag_ignores_even_invalid_environment(self):
        result = subprocess.run([sys.executable, "-m", "examples.i1_live_check"],
                                env={"LLM_BASE_URL": "invalid-"+KEY}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No requests sent", result.stdout)
        self.assertNotIn(KEY, result.stdout+result.stderr)

    def test_live_check_with_missing_configuration_stops_before_network(self):
        result = subprocess.run([sys.executable, "-m", "examples.i1_live_check", "--live"],
                                env={}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["stage"], "configuration")
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
