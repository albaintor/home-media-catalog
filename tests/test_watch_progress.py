"""Regression coverage for shared streaming/catalogue watch progress."""
from home_media_catalog.watch_progress import (
    decode_episodes,
    effective_series_status,
    encode_episodes,
)


def test_episode_snapshot_roundtrip():
    known = {(1, 1), (1, 2), (1, 3), (2, 1), (2, 4)}
    assert encode_episodes(known) == "1=1-3;2=1,4"
    assert decode_episodes(encode_episodes(known)) == known


def test_future_season_does_not_hide_a_previously_completed_series():
    watched = {"watched": "true", "watched_episode_snapshot": "1=1-8;2=1-8"}
    current = {"episodes": "1=1-8;2=1-8"}
    newly_aired = {"episodes": "1=1-8;2=1-8;3=1"}
    assert effective_series_status(watched, current) == "watched"
    assert effective_series_status(watched, newly_aired) == "in_progress"
    assert watched["watched_episode_snapshot"] == "1=1-8;2=1-8"


def test_episode_override_completes_a_new_season():
    watched = {"watched": "true", "watched_episode_snapshot": "1=1-8",
               "episode_overrides": '{"2:1":true}'}
    assert effective_series_status(
        watched, {"episodes": "1=1-8;2=1"}) == "watched"
    watched["episode_overrides"] = '{"2:1":false}'
    assert effective_series_status(
        watched, {"episodes": "1=1-8;2=1"}) == "in_progress"


def test_specials_and_unreleased_episodes_are_not_required():
    watched = {"watched": "true", "watched_episode_snapshot": "1=1-2"}
    # The inventory deliberately contains only aired episodes and excludes S0.
    assert effective_series_status(
        watched, {"episodes": "1=1-2"}) == "watched"


def test_legacy_status_is_never_assumed_complete_without_retrofit():
    assert effective_series_status(
        {"watched": "true"}, {"episodes": "1=1-8;2=1"}) is None
    assert effective_series_status(
        {"watched": "false"}, {"episodes": "1=1-8"}) == "unwatched"
