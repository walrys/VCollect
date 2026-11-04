import httpx

from app.api import health


def test_startup_creates_the_database(client, settings):
    assert settings.db_path.exists()


def test_gallery_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "VCOLLECT" in response.text


def test_page_files_are_checked_for_a_newer_copy_on_every_load(client):
    # No build step means no versioned file names: without this header a browser keeps
    # running an old gallery.js against a newer API.
    for path in ("/", "/gallery.js", "/app.css", "/logos/reddit.svg"):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-cache", path

    # The check costs no download while the file is unchanged.
    etag = client.get("/gallery.js").headers["etag"]
    assert client.get("/gallery.js", headers={"If-None-Match": etag}).status_code == 304


def test_health_reports_unconfigured_and_unreachable(client):
    body = client.get("/api/health").json()
    assert body == {
        "notion": {"configured": False, "error": None},
        "ollama": {"reachable": False, "model": "qwen3.5:9b", "model_ready": False},
        "whisper": {"reachable": False},
    }


def test_health_reports_a_pulled_model(client, monkeypatch):
    def fake_services(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]})
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        health,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake_services), **kwargs),
    )

    body = client.get("/api/health").json()
    assert body["ollama"] == {"reachable": True, "model": "qwen3.5:9b", "model_ready": True}
    assert body["whisper"] == {"reachable": True}
