from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402
from backend.settings import Settings  # noqa: E402
from core.contracts import ConsultationRequest, ConsultationResult  # noqa: E402
from core.stub_engine import StubEngine  # noqa: E402


class RecordingEngine(StubEngine):
    """StubEngine, запоминающий все запросы — для проверки изоляции контекста."""

    def __init__(self):
        self.requests: list[ConsultationRequest] = []

    def consult(self, request: ConsultationRequest) -> ConsultationResult:
        self.requests.append(request.model_copy(deep=True))
        return super().consult(request)


class GateEngine(StubEngine):
    """Engine, ждущий разрешения — для тестов конкурентных ходов и отмены."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def consult(self, request):
        if "__gate__" in request.message:
            self.entered.set()
            assert self.release.wait(5)
        return super().consult(request)


class Api:
    """Тонкая обёртка над TestClient с токеном вкладки."""

    def __init__(self, client: TestClient):
        self.c = client

    def new_session(self, close: tuple[str, str] | None = None):
        headers, body = {}, {}
        if close:
            body = {"close_session_id": close[0]}
            headers = {"X-Client-Token": close[1]}
        r = self.c.post("/api/v1/sessions", json=body, headers=headers)
        assert r.status_code == 201, r.text
        d = r.json()
        return d["session_id"], d["client_token"]

    def say(self, sid, tok, text, request_id=None, expect=200):
        r = self.c.post(f"/api/v1/sessions/{sid}/messages", headers={"X-Client-Token": tok},
                        json={"request_id": request_id or str(uuid.uuid4()), "text": text})
        assert r.status_code == expect, r.text
        return r.json()

    def get(self, path, tok=None, **kw):
        return self.c.get(path, headers={"X-Client-Token": tok} if tok else {}, **kw)

    def post(self, path, tok=None, **kw):
        return self.c.post(path, headers={"X-Client-Token": tok} if tok else {}, **kw)

    def put(self, path, tok=None, **kw):
        return self.c.put(path, headers={"X-Client-Token": tok} if tok else {}, **kw)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "db.sqlite3"), leads_dir=str(tmp_path / "leads"),
                    engine_spec=None, debug_api=True, archive_token="test-archive-token",
                    cors_origins=["http://localhost:5173"])


@pytest.fixture
def make_api(settings):
    apps = []

    def _make(engine=None, **overrides):
        s = Settings(**{**settings.__dict__, **overrides})
        app = create_app(s, engine=engine)
        apps.append(app)
        return Api(TestClient(app)), app

    yield _make
    for a in apps:
        a.state.db.close()


@pytest.fixture
def api(make_api):
    return make_api()[0]
