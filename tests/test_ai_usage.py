"""OpenAI metering, cost guard, index hooks and legacy settings migration."""

import asyncio

import httpx
import pytest
from conftest import READ

from home_media_catalog.ai_usage import (
    AIBudgetExceeded,
    AIPriceUnavailable,
    AIUsageLedger,
    tracked_post,
    usage_session,
)


class FakeOpenAIClient:
    def __init__(self, data=None):
        self.calls = []
        self.payload = data or {"usage": {"input_tokens": 100, "output_tokens": 50}}

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "model": json["model"]})
        return httpx.Response(200, json=self.payload)


def test_ledger_counts_true_tokens_and_blocks_new_requests(tmp_path):
    ledger = AIUsageLedger(tmp_path)
    client = FakeOpenAIClient()
    payload = {"model": "gpt-6-luna", "input": "hello", "max_output_tokens": 400}
    with usage_session(ledger, 0.001, "search"):
        first = asyncio.run(
            tracked_post(client, "https://api.openai.com/v1/responses", headers={}, json=payload)
        )
        assert first.status_code == 200
        assert len(client.calls) == 1
        report = ledger.status(0.001)
        assert report["categories"]["search"]["input_tokens"] == 100
        assert report["categories"]["search"]["output_tokens"] == 50
        assert report["categories"]["search"]["requests"] == 1
        assert report["categories"]["search"]["cost_usd"] == pytest.approx(0.000035)
        assert report["remaining_usd"] > 0
        # Conservative reservation is charged before sending additional tokens.
        with pytest.raises(AIBudgetExceeded):
            asyncio.run(
                tracked_post(
                    client,
                    "https://api.openai.com/v1/responses",
                    headers={},
                    json={**payload, "max_output_tokens": 3000},
                )
            )
        assert len(client.calls) == 1
    assert ledger.path.stat().st_mode & 0o777 == 0o600


def test_ledger_meter_embedding_tokens_and_index_category(tmp_path):
    ledger = AIUsageLedger(tmp_path)
    client = FakeOpenAIClient({"usage": {"prompt_tokens": 125, "total_tokens": 125}})
    with usage_session(ledger, 5, "indexing"):
        asyncio.run(
            tracked_post(
                client,
                "https://api.openai.com/v1/embeddings",
                headers={},
                json={"model": "text-embedding-3-small", "input": ["short synopsis"]},
            )
        )
    stats = ledger.status(5)
    assert stats["categories"]["indexing"]["input_tokens"] == 125
    assert stats["categories"]["indexing"]["cost_usd"] == pytest.approx(0.0000025)
    assert stats["categories"].get("search") is None


def test_custom_model_fails_closed_before_a_billable_api_request(tmp_path):
    ledger = AIUsageLedger(tmp_path)
    client = FakeOpenAIClient()
    with usage_session(ledger, 5, "search"):
        with pytest.raises(AIPriceUnavailable):
            asyncio.run(
                tracked_post(
                    client,
                    "https://api.openai.com/v1/responses",
                    headers={},
                    json={"model": "unknown-custom-model", "input": "test"},
                )
            )
    assert not client.calls


def test_budget_settings_persist_and_reports_are_admin_only(client, admin):
    readonly = {"Authorization": f"Bearer {READ}"}
    assert client.get("/api/v1/ai/usage", headers=readonly).status_code == 403
    update = client.put(
        "/api/v1/ai/settings",
        headers=admin,
        json={"model": "gpt-6-luna", "monthly_budget_usd": 1.5},
    )
    assert update.status_code == 200, update.text
    assert update.json()["monthly_budget_usd"] == 1.5
    assert update.json()["usage"]["monthly_budget_usd"] == 1.5
    # Older web/iOS clients must not reset an existing budget on model edit.
    second = client.put("/api/v1/ai/settings", headers=admin, json={"model": "gpt-4o-mini"})
    assert second.json()["monthly_budget_usd"] == 1.5
    assert client.get("/api/v1/ai/usage", headers=admin).json()["month"]


def test_catalogue_sync_trigger_is_connected_without_prolonging_imports():
    from pathlib import Path

    sync = (Path(__file__).parent.parent / "home_media_catalog" / "sync.py").read_text()
    assert 'result.get("rebuilt_works") and self.on_catalog_changed' in sync
    assert "self.on_catalog_changed()" in sync
    app = (Path(__file__).parent.parent / "home_media_catalog" / "app.py").read_text()
    assert "sync.on_catalog_changed = on_catalog_changed" in app
    assert "start_ai_index()" in app
    assert "start_artwork_repair()" in app
    assert "run_ai_index_debounced" in app
