"""Personalized streaming AI uses only server-owned watch state and real SVOD offers."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from home_media_catalog import ai_streaming
from home_media_catalog.store import Store
from home_media_catalog.streaming import StreamingStateRow, _merge


def state(key, payload):
    return {
        "key": key, "payload": payload, "deleted": False,
    }


def test_profile_merges_independent_streaming_ratings_and_series_progress():
    result = ai_streaming._build_profile(
        [
            {"kind": "movie", "tmdb_id": "11", "title": "Local",
             "genres": '["Fantasy"]', "user_rating": 5.0, "watch_status": "watched"},
            {"kind": "tvshow", "tmdb_id": "22", "title": "Series",
             "genres": "[]", "user_rating": None, "watch_status": "unknown"},
        ],
        [
            state("personal:movie:11", {"rating": "9.5", "watched": "true"}),
            state("personal:tv:22", {
                "watched": "false", "watched_episode_snapshot": "1=1-3",
                "episode_overrides": '{"1:4":true}',
            }),
            state("personal:tv:33", {"rating": "8", "watched": "true"}),
        ],
    )
    assert result["works"]["movie:11"]["rating"] == 9.5
    assert result["rated_count"] == 2
    assert {"movie:11", "tv:22", "tv:33"} <= result["seen"]
    assert any(row["id"] == "tv:33" for row in result["positive"])


def test_profile_cleared_rating_takes_precedence_over_catalogue():
    profile = ai_streaming._build_profile(
        [{"kind": "movie", "tmdb_id": "12", "title": "Old", "genres": "[]",
          "user_rating": 9, "watch_status": "unknown"}],
        [state("personal:movie:12", {"rating_cleared": "true", "watched": "false"})],
    )
    assert profile["rated_count"] == 0
    assert profile["works"]["movie:12"]["rating"] is None


def test_score_rewards_positive_genre_and_penalizes_disliked():
    row = {"genre_ids": [18], "vote_average": 8.2, "vote_count": 500,
           "overview": "A tense story about a detective", "title": "Mystery"}
    preferred = ai_streaming._score(row, {18: 1.2}, "tense detective")
    rejected = ai_streaming._score(row, {18: -1.2}, "tense detective")
    assert preferred > rejected + 2
    assert ai_streaming._language_hint("thrillers coréens") == "ko"


def test_recommendations_exclude_watched_and_verify_region_subscription(tmp_path, monkeypatch):
    store = Store(tmp_path / "catalogue.sqlite")
    store.initialize()
    stamp = datetime(2026, 11, 22, tzinfo=UTC).isoformat()
    _merge(store, [
        StreamingStateRow(
            key="personal:tv:10", payload={"watched": "true", "rating": "9"},
            modified_at=stamp,
        ),
        StreamingStateRow(
            key="personal:tv:20", payload={"watched": "true", "rating": "3"},
            modified_at=stamp,
        ),
    ])
    queried = []

    async def fake_tmdb(token, path, params):
        queried.append((path, params))
        if path == "/tv/10":
            return {"genres": [{"id": 18}]}
        if path == "/tv/20":
            return {"genres": [{"id": 10759}]}
        if path.startswith("/discover/"):
            assert params["watch_region"] == "FR"
            assert params["with_watch_monetization_types"] == "flatrate"
            return {"results": [
                {"id": 10, "name": "Watched", "vote_average": 9, "genre_ids": [18]},
                {"id": 30, "name": "Recommended", "original_name": "Recommended",
                 "overview": "A personal drama", "genre_ids": [18], "vote_average": 8.5,
                 "vote_count": 300, "first_air_date": "2025-01-01",
                 "original_language": "ko"},
                {"id": 40, "name": "Not subscribed", "original_name": "Not subscribed",
                 "overview": "Another drama", "genre_ids": [18], "vote_average": 9,
                 "vote_count": 300, "first_air_date": "2025-01-01",
                 "original_language": "ko"},
            ]}
        return {"results": []}

    async def fake_availability(token, kind, tmdb_id, selection, region):
        assert selection == "netflix"
        assert region == "FR"
        return ["netflix"] if tmdb_id == 30 else []

    async def fake_explain(prompt, favorites, dislikes, candidates,
                           model, key, *, provider, mixed=None):
        assert "Personal viewing profile" in prompt
        assert mixed == []
        assert {entry["id"] for entry in candidates} == {"tv:30"}
        assert favorites and favorites[0]["user_rating"] == 9
        return (
            [{"id": "tv:30", "reason": "Related to your highly rated dramas."}],
            "You enjoyed character-driven dramas, based on your 9/10 rating.",
        )

    monkeypatch.setattr(ai_streaming, "_tmdb", fake_tmdb)
    monkeypatch.setattr(ai_streaming, "_availability", fake_availability)
    monkeypatch.setattr(ai_streaming, "explain_streaming_recommendations", fake_explain)
    request = ai_streaming.StreamingAIInput(
        prompt="Recommend Korean shows I haven't watched",
        kind="tv", services=["netflix"], region="FR",
    )
    plan = SimpleNamespace(
        kind="tvshow", year_min=None, year_max=None,
        rating_min=None, semantic_query="dramatic series", retrieval_terms=[],
    )
    result = asyncio.run(ai_streaming.recommend_streaming(
        store, "test-token", request, plan,
        model="gpt-4o-mini", api_key="test", provider="openai",
    ))
    assert [item["id"] for item in result["items"]] == [30]
    assert result["items"][0]["service_keys"] == ["netflix"]
    assert result["relevance_reasons"]["tv:30"].startswith("Related")
    assert result["profile_count"] == 2
    assert "character-driven dramas" in result["taste_summary"]


def test_no_profile_does_not_generate_random_recommendations(tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    store.initialize()
    result = asyncio.run(ai_streaming.recommend_streaming(
        store, "tmdb-test", ai_streaming.StreamingAIInput(
            prompt="What should I watch?", services=["netflix"],
        ),
        SimpleNamespace(kind="tvshow"),
        model="gpt-4o-mini", api_key="test", provider="openai",
    ))
    assert result["items"] == []
    assert result["profile_count"] == 0


def test_streaming_explainer_preserves_model_order_and_grounded_ids(monkeypatch):
    calls = []

    async def fake_generate(instructions, user_input, schema, model, key,
                            *, provider, max_output_tokens, schema_name):
        import json
        context = json.loads(user_input)
        calls.append(context)
        assert "ratings" in instructions
        assert context["liked"][0]["title"] == "Alchemy of Souls"
        return {
            "taste_summary": "Your 10/10 for Alchemy of Souls points to fantasy romance.",
            "matches": [
                {"id": "tv:3", "reason": "A character-driven fantasy with a romantic arc."},
                {"id": "tv:10000", "reason": "Invented"},
                {"id": "tv:1", "reason": "More tension and a strong central couple."},
                {"id": "tv:3", "reason": "Duplicate"},
            ],
        }

    monkeypatch.setattr(ai_streaming, "generate_json", fake_generate)
    matches, summary = asyncio.run(ai_streaming.explain_streaming_recommendations(
        "Recommend K-dramas in my tastes",
        [{"title": "Alchemy of Souls", "user_rating": 10,
          "genres": ["Fantasy"], "synopsis": "Magic and romance"}],
        [],
        [{"id": "tv:1", "title": "One", "synopsis": "A tense romance"},
         {"id": "tv:3", "title": "Three", "synopsis": "A fantasy romance"}],
        "gpt-4o-mini", "test", provider="openai",
    ))
    assert [item["id"] for item in matches] == ["tv:3", "tv:1"]
    assert "Alchemy of Souls" in summary
    assert len(calls) == 1


def test_kdrama_hint_detects_spaced_and_hyphenated_forms():
    for phrase in ("K drama", "k-drama", "kdrama", "K-dramas",
                   "dramas coréens", "Korean romantic shows"):
        assert ai_streaming._language_hint(phrase) == "ko"


def test_adaptive_context_prioritizes_series_and_caps_detail_lookups():
    def work(kind, number, rating):
        return {"id": f"{kind}:{number}", "title": f"Work {number}",
                "rating": rating, "genres": [], "seen": True}

    profile = {
        "positive": [work("movie", number, 10) for number in range(1, 40)]
                    + [work("tv", number, 9.5) for number in range(40, 60)],
        "negative": [work("tv", number, 2) for number in range(60, 68)],
        "mixed": [work("tv", number, 6) for number in range(70, 77)],
    }
    liked, disliked, neutral = ai_streaming._profile_evidence_selection(
        profile, ("tv",)
    )
    assert len(liked) <= ai_streaming._profile_context_budget(profile) <= 30
    assert len(disliked) <= 5
    assert len(neutral) <= 4
    assert all(item["id"].startswith("tv:") for item in liked[:12])
    assert any(item["id"].startswith("movie:") for item in liked)
    assert liked[0]["id"] == "tv:40"


def test_context_budget_scales_without_manual_settings():
    def profile(n):
        return {"positive": [{"id": f"tv:{i}"} for i in range(n)],
                "negative": [], "mixed": []}

    assert ai_streaming._profile_context_budget(profile(2)) >= 10
    assert ai_streaming._profile_context_budget(profile(80)) > (
        ai_streaming._profile_context_budget(profile(2))
    )
    assert ai_streaming._profile_context_budget(profile(1000)) == 30


def test_moderate_ratings_are_context_not_dislikes():
    profile = ai_streaming._build_profile([
        {"kind": "tvshow", "tmdb_id": "1", "title": "Mixed",
         "genres": "[]", "user_rating": 6, "watch_status": "watched"},
        {"kind": "tvshow", "tmdb_id": "2", "title": "Loved",
         "genres": "[]", "user_rating": 10, "watch_status": "watched"},
    ], [])
    assert [row["id"] for row in profile["mixed"]] == ["tv:1"]
    assert [row["id"] for row in profile["positive"]] == ["tv:2"]
    assert profile["negative"] == []
