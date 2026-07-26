"""Last-seen value cache.

The point of this cache: the report stream is delta-only and fields like
packState are broadcast rarely, so a restart used to mean staring at "--"
until the hub felt like sending one. These tests pin both halves - the
values do come back, and they come back honestly labelled as old.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zendure_mqtt_viewer import layout, persist
from zendure_mqtt_viewer.state import DashboardState

T0 = 1784980800.0


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "last-seen.json"


def _busy_state() -> DashboardState:
    state = DashboardState()
    state.apply_payload(
        {
            "timestamp": 1784980052,
            "properties": {
                "packState": 1,
                "outputPackPower": 20,
                "electricLevel": 76,
                "minSoc": 200,
                "socSet": 1000,
            },
            "packData": [{"sn": "ZZ0EXAMPLE00001", "soh": 978, "maxTemp": 3071}],
        },
        wall_time=T0,
    )
    return state


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_rare_fields_survive_a_restart(cache):
    persist.save(_busy_state(), cache, now=T0)

    fresh = DashboardState()
    assert fresh.hub.get("packState") is None  # the old starting point

    restored = persist.load(fresh, cache, now=T0 + 60)
    assert restored > 0
    assert fresh.hub["packState"].raw == 1
    assert fresh.hub["minSoc"].raw == 200
    assert fresh.hub["socSet"].raw == 1000


def test_pack_values_survive_too(cache):
    persist.save(_busy_state(), cache, now=T0)
    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 60)
    assert fresh.pack_order == ["ZZ0EXAMPLE00001"]
    assert fresh.packs["ZZ0EXAMPLE00001"]["soh"].raw == 978


def test_restored_values_keep_their_original_age(cache):
    # The whole honesty requirement: a restored reading is old, and the UI
    # must be able to say so rather than passing it off as fresh.
    persist.save(_busy_state(), cache, now=T0)
    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 3600)
    assert fresh.hub["packState"].wall_time == T0
    assert layout.is_stale(now=T0 + 3600, wall_time=fresh.hub["packState"].wall_time)


def test_restoring_does_not_fake_message_traffic(cache):
    persist.save(_busy_state(), cache, now=T0)
    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 60)
    assert fresh.messages_received == 0
    assert fresh.parse_errors == 0
    # Nothing has arrived in *this* run, so the header must not claim it has.
    assert fresh.last_message_wall_time is None


def test_live_messages_overwrite_restored_values(cache):
    persist.save(_busy_state(), cache, now=T0)
    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 60)
    fresh.apply_payload({"timestamp": 2, "properties": {"electricLevel": 91}}, wall_time=T0 + 60)
    assert fresh.hub["electricLevel"].raw == 91
    assert fresh.hub["electricLevel"].wall_time == T0 + 60
    assert fresh.hub["packState"].raw == 1  # untouched by the new delta


def test_display_text_is_re_derived_not_replayed_from_the_file(cache):
    # Only raw values are persisted, so a decode/label fix reaches restored
    # values instead of the cache resurrecting the old wording.
    persist.save(_busy_state(), cache, now=T0)
    on_disk = json.loads(cache.read_text())
    assert "display" not in json.dumps(on_disk["state"]["hub"]["packState"])

    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 60)
    assert fresh.hub["packState"].display == "Charging (1)"


# ---------------------------------------------------------------------------
# A bad cache must never stop the dashboard from starting
# ---------------------------------------------------------------------------


def test_missing_file_is_not_an_error(cache):
    state = DashboardState()
    assert persist.load(state, cache) == 0
    assert state.hub == {}


def test_corrupt_json_is_ignored(cache):
    cache.write_text("{not json at all")
    state = DashboardState()
    assert persist.load(state, cache) == 0


def test_truncated_but_valid_json_is_ignored(cache):
    cache.write_text('{"version": 1}')
    state = DashboardState()
    assert persist.load(state, cache) == 0


def test_unknown_schema_version_is_ignored(cache):
    persist.save(_busy_state(), cache, now=T0)
    payload = json.loads(cache.read_text())
    payload["version"] = persist.SCHEMA_VERSION + 99
    cache.write_text(json.dumps(payload))
    state = DashboardState()
    assert persist.load(state, cache) == 0


def test_entries_with_junk_in_them_are_skipped_individually(cache):
    persist.save(_busy_state(), cache, now=T0)
    payload = json.loads(cache.read_text())
    payload["state"]["hub"]["packState"] = "not a record"
    payload["state"]["hub"]["minSoc"] = {"raw": 200}  # no wall_time
    cache.write_text(json.dumps(payload))

    state = DashboardState()
    assert persist.load(state, cache, now=T0 + 60) > 0
    assert "packState" not in state.hub
    assert "minSoc" not in state.hub
    assert state.hub["electricLevel"].raw == 76  # the good ones still land


def test_stale_cache_is_not_restored(cache):
    persist.save(_busy_state(), cache, now=T0)
    state = DashboardState()
    assert persist.load(state, cache, now=T0 + persist.MAX_AGE_SECONDS + 1) == 0


def test_cache_just_inside_the_age_limit_is_restored(cache):
    persist.save(_busy_state(), cache, now=T0)
    state = DashboardState()
    assert persist.load(state, cache, now=T0 + persist.MAX_AGE_SECONDS - 1) > 0


def test_unwritable_location_does_not_raise(tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("in the way")
    assert persist.save(_busy_state(), blocked / "nested" / "cache.json") is False


def test_empty_state_does_not_clobber_a_good_cache(cache):
    persist.save(_busy_state(), cache, now=T0)
    before = cache.read_text()
    assert persist.save(DashboardState(), cache, now=T0 + 10) is False
    assert cache.read_text() == before


def test_save_is_atomic_leaving_no_temp_files(cache):
    persist.save(_busy_state(), cache, now=T0)
    assert [p.name for p in cache.parent.iterdir()] == [cache.name]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_explicit_path_wins(tmp_path):
    assert persist.resolve_cache_path(str(tmp_path / "x.json")) == tmp_path / "x.json"


def test_env_var_overrides_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv(persist.ENV_CACHE_PATH, str(tmp_path / "env.json"))
    assert persist.resolve_cache_path() == tmp_path / "env.json"


def test_default_path_follows_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.delenv(persist.ENV_CACHE_PATH, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert persist.resolve_cache_path() == tmp_path / "zendure-mqtt-viewer" / "last-seen.json"


# ---------------------------------------------------------------------------
# End to end through the layout
# ---------------------------------------------------------------------------


def test_restarted_dashboard_shows_the_rare_field_instead_of_dashes(cache):
    persist.save(_busy_state(), cache, now=T0)

    fresh = DashboardState()
    persist.load(fresh, cache, now=T0 + 120)
    frame = layout.build_frame(fresh, "hub", 100, 27, now=T0 + 120, mode="live")
    text = frame.to_text()
    assert "Charging" in text
