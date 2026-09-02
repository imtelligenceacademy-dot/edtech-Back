"""The schema must not be readable in production.

`docs_url=None` hides the Swagger page. It does not hide `/openapi.json`, which
FastAPI serves independently — so the docs looked disabled in production while
the schema behind them, every route and field, stayed public. This pins all
three together so hiding one again cannot leave another open.
"""

from __future__ import annotations

from app.main import doc_urls


def test_production_serves_no_docs_and_no_schema():
    urls = doc_urls(is_production=True)

    assert urls["docs_url"] is None
    assert urls["redoc_url"] is None
    assert urls["openapi_url"] is None, (
        "the schema is the map of every admin route; hiding /docs alone leaves it public"
    )


def test_development_keeps_the_docs_and_the_schema():
    urls = doc_urls(is_production=False)

    assert urls["docs_url"] == "/docs"
    assert urls["openapi_url"] == "/openapi.json"
