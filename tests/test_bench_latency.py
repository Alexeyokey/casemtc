"""Замер S5 отличает сборку цели и проверяет реальное сохранение заявки."""
from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
import pytest

from scripts import bench_latency


def test_nearest_rank_percentiles():
    assert bench_latency.pct([], 95) is None
    assert bench_latency.pct([2.0, 1.0], 50) == 1.0
    assert bench_latency.pct(list(range(1, 21)), 95) == 19


def test_target_build_metadata():
    assert bench_latency.target_build({}, None) == (None, "unknown")
    assert bench_latency.target_build({}, "abcdef1") == ("abcdef1", "provided")
    assert bench_latency.target_build({"build_sha": "ABCDEF2"}, "abcdef2") == ("abcdef2", "health")
    with pytest.raises(ValueError, match="различаются"):
        bench_latency.target_build({"build_sha": "abcdef2"}, "abcdef1")


def test_real_in_process_lead_save(monkeypatch):
    monkeypatch.setenv("CONSULTATION_ENGINE", "core.stub_engine:StubEngine")
    monkeypatch.delenv("CASEMTC_BUILD_SHA", raising=False)
    report = bench_latency.run(None, 1)
    assert report["is_stub_engine"]
    assert report["target_build_sha"] is None
    assert report["errors"] == 0, report["error_samples"]
    assert report["client_total"]["n"] == report["turns"] == report["debug_traces"]
    assert report["lead_save"]["n"] == 1


def test_remote_report_keeps_local_sha_separate_and_reports_failed_save(monkeypatch):
    seen = []

    def serve(request):
        path = request.url.path
        if path == "/api/v1/health":
            return httpx.Response(200, json={"engine": "remote:RagEngine"})
        if path == "/api/v1/sessions":
            return httpx.Response(201, json={"session_id": "bench-session", "client_token": "bench-token"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"status": "completed", "debug_available": False})
        if path.endswith("/lead"):
            return httpx.Response(200, json={"status": "ready", "offer_id": "bench-offer"})
        if path.endswith("/lead/save"):
            seen.append(json.loads(request.content))
            return httpx.Response(502, text="bad gateway")
        raise AssertionError(path)

    @contextmanager
    def make_client(url):
        with httpx.Client(base_url=url, transport=httpx.MockTransport(serve)) as client:
            yield client

    monkeypatch.setattr(bench_latency, "make_client", make_client)
    monkeypatch.setattr(bench_latency, "git_sha", lambda: "local-checkout-sha")
    monkeypatch.setattr(bench_latency, "SCENARIOS", {"to_lead_phone_only": ["test message"]})
    report = bench_latency.run("http://benchmark.example", 1)

    assert report["benchmark_git_sha"] == "local-checkout-sha"
    assert report["target_build_sha"] is None and report["target_build_sha_source"] == "unknown"
    assert report["lead_save"]["n"] == 0 and report["error_rate"] == 0.25
    assert report["error_samples"] == [{"scenario": "to_lead_phone_only", "step": "lead_save", "status": 502,
                                        "code": "unexpected_response"}]
    assert seen[0]["offer_id"] == "bench-offer" and seen[0]["idempotency_key"]
