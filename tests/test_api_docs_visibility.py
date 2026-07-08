from fastapi.testclient import TestClient

from app.main import app


def test_default_openapi_docs_are_not_exposed():
    client = TestClient(app)

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_page_only_exposes_journal_query_debugger():
    client = TestClient(app)

    response = client.get("/api-docs")

    assert response.status_code == 200
    assert "GET /api/journals" in response.text
    assert "/api/journals/{journal_id}" not in response.text
    assert "OpenAPI" not in response.text
