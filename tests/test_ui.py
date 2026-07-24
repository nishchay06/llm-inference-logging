"""The chatbot serves a frontend at '/'.

Which page depends on the environment: the built React app (frontend/dist) when
present, else the legacy plain-HTML page. Either way it must be an HTML document;
the UI behaviour itself is verified in the browser.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_index_serves_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_legacy_static_assets_still_mounted():
    # The /static mount (legacy plain-HTML fallback's marked.min.js) stays wired.
    client = TestClient(app)
    resp = client.get("/static/marked.min.js")
    assert resp.status_code == 200
    assert "marked" in resp.text
