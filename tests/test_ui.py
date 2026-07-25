"""The chatbot serves the built React chat UI at '/'.

There is no server-rendered fallback page: the UI is a Vite build, so what '/'
returns depends on whether that build exists in this environment (Docker and a
local `npm run build` produce it; a bare checkout does not). Both outcomes are a
contract worth asserting — the built app, or a 503 that says how to build it —
and neither is ever a bare 404. The UI's behaviour itself is verified in the
browser, not here.
"""

from fastapi.testclient import TestClient

from app.main import FRONTEND_DIST, app


def test_root_serves_ui_or_explains_how_to_build():
    client = TestClient(app)
    resp = client.get("/")
    assert "text/html" in resp.headers["content-type"]

    if FRONTEND_DIST.is_dir():
        assert resp.status_code == 200
    else:
        assert resp.status_code == 503
        assert "npm run build" in resp.text


def test_api_routes_take_precedence_over_the_frontend_mount():
    """The frontend is mounted at '/', so it must not shadow the API."""
    client = TestClient(app)
    assert client.get("/hello").json() == {"message": "hello, world"}
    assert client.get("/providers").status_code == 200
