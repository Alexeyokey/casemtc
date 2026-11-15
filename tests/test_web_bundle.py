"""Раздача React-сборки web/dist backend'ом и откат на демо-чат."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.settings import Settings


def _client(tmp_path, monkeypatch, dist=None):
    if dist is None:
        monkeypatch.setenv("CASEMTC_WEB_DIST", str(tmp_path / "missing"))
    else:
        monkeypatch.setenv("CASEMTC_WEB_DIST", str(dist))
    app = create_app(Settings(db_path=str(tmp_path / "db.sqlite3"), leads_dir=str(tmp_path / "leads")))
    return TestClient(app), app


def test_serves_react_build_when_present(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>Консультант МТС EnergyTool</title><div id=root>", encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    client, app = _client(tmp_path, monkeypatch, dist)
    page = client.get("/")
    assert page.status_code == 200 and 'id=root' in page.text
    assert page.headers["cache-control"] == "no-store"
    asset = client.get("/assets/index-abc.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert "chat.js" not in page.text or "demo.js" not in page.text
    assert client.get("/demo").status_code == 200  # прежний демо-чат доступен
    app.state.db.close()


def test_falls_back_to_demo_without_build(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    page = client.get("/")
    assert page.status_code == 200 and "/demo.js" in page.text
    assert client.get("/assets/x.js").status_code == 404
    app.state.db.close()
