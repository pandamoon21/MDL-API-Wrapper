"""OpenAPI response schemas are docs-only: they show in /docs but never filter output."""
import pytest
from fastapi.testclient import TestClient

import mdlaw

DOCUMENTED = [
    ("get", "/api/v1/titles/{tid}", "TitleDetail"),
    ("post", "/api/v1/search", "TitleSummary"),
    ("get", "/api/v1/browse", "TitleSummary"),
    ("get", "/api/v1/people/{pid}", "PersonDetail"),
    ("get", "/api/v1/health", "Health"),
    ("get", "/api/v1/status", "Dashboard"),
    ("get", "/api/v1/dashboard", "Dashboard"),
    ("get", "/api/v1/cache/stats", "CacheStats"),
    ("get", "/api/v1/auth/status", "AuthStatus"),
]


def _ref(schema: dict) -> str:
    return (schema.get("items") or schema).get("$ref", "")


@pytest.mark.parametrize("method,path,model", DOCUMENTED)
def test_openapi_routes_reference_their_schemas(method, path, model):
    spec = mdlaw.app.openapi()
    responses = spec["paths"][path][method]["responses"]
    ok = responses["200"]["content"]["application/json"]["schema"]
    assert _ref(ok) == f"#/components/schemas/{model}"
    assert spec["components"]["schemas"][model]["additionalProperties"] is True
    if path != "/api/v1/health":
        for code in ("4XX", "5XX"):
            err = responses[code]["content"]["application/json"]["schema"]
            assert _ref(err) == "#/components/schemas/ErrorResponse"


def test_documented_routes_pass_upstream_fields_through(monkeypatch):
    """No response_model: unknown upstream fields and odd types reach the client as-is."""
    upstream = {"id": 686, "title": "X", "rating": "not-a-number", "brand_new_field": {"a": [1]}}

    async def fake_fetch(*a, **k):
        return upstream
    monkeypatch.setattr(mdlaw, "fetch", fake_fetch)
    with TestClient(mdlaw.app) as cl:
        r = cl.get("/api/v1/titles/686")
    assert r.status_code == 200
    assert r.json() == upstream


def test_error_body_matches_documented_shape():
    with TestClient(mdlaw.app) as cl:
        r = cl.post("/api/v1/search", params={"q": "   "})
    assert r.status_code == 400
    assert r.json() == {"detail": {"error": True, "code": 400, "detail": "query param q is required"}}
