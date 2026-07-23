"""The chatbot serves a UI shell at '/'.

The HTML/JS behaviour is verified in the browser; here we only assert the
static page is wired up and served (no DB needed for this route).
"""

from fastapi.testclient import TestClient

from app.main import app


def test_index_is_served():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # A marker from our page so we know it's our UI, not a default.
    assert "Conversations" in resp.text
