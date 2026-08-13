"""Phase A2.1 — configurable search freshness (Bing only).

Scope of this phase:
- Make Bing `freshness` configurable via DISCOVERY_SEARCH_FRESHNESS.
- Default "Week" must be preserved (zero behavior change).
- Invalid values fall back to "Week" with a warning, never crash.
- DDG fallback is untouched and does not support freshness.

These tests do NOT modify score/threshold/search volume/keywords/dedup.
"""

import importlib

import main


# ---------------------------------------------------------------------------
# Helper: re-evaluate the freshness config logic the same way main.py does at
# import time. We replicate the exact rule so we can unit-test invalid-value
# behavior deterministically without import-time side effects.
# ---------------------------------------------------------------------------
def _resolve_freshness(raw):
    allowed = ("Day", "Week", "Month")
    if raw not in allowed:
        return "Week", False  # (value, ok)
    return raw, True


def test_default_constant_is_week():
    # Guard against silent default changes in the future.
    assert main.DISCOVERY_SEARCH_FRESHNESS == "Week"
    assert main._ALLOWED_FRESHNESS == ("Day", "Week", "Month")


def test_valid_day_passes_through():
    val, ok = _resolve_freshness("Day")
    assert ok is True
    assert val == "Day"


def test_valid_month_passes_through():
    val, ok = _resolve_freshness("Month")
    assert ok is True
    assert val == "Month"


def test_invalid_value_falls_back_to_week():
    val, ok = _resolve_freshness("InvalidValue")
    assert ok is False
    assert val == "Week"


def test_invalid_empty_falls_back_to_week():
    val, ok = _resolve_freshness("")
    assert ok is False
    assert val == "Week"


def test_bing_request_uses_configured_freshness(monkeypatch):
    """Actual Bing request params must carry the configured freshness value."""
    sentinel = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"webPages": {"value": []}}

    def fake_get(url, headers=None, params=None, timeout=None):
        sentinel["params"] = params
        return _Resp()

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main, "DISCOVERY_SEARCH_FRESHNESS", "Day")
    monkeypatch.setattr(main, "SEARCH_PER_KEYWORD", 3)

    main.bing_search("looking for CNC machining", "dummy-key")

    assert "params" in sentinel
    assert sentinel["params"]["freshness"] == "Day"


def test_bing_request_default_freshness_is_week(monkeypatch):
    """Without overriding the constant, the request must use 'Week'."""
    sentinel = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"webPages": {"value": []}}

    def fake_get(url, headers=None, params=None, timeout=None):
        sentinel["params"] = params
        return _Resp()

    monkeypatch.setattr(main.requests, "get", fake_get)
    monkeypatch.setattr(main, "SEARCH_PER_KEYWORD", 3)

    main.bing_search("looking for CNC machining", "dummy-key")

    assert sentinel["params"]["freshness"] == "Week"


def test_module_reload_picks_up_env(monkeypatch):
    """Import-time evaluation: setting the env before import must win."""
    monkeypatch.setenv("DISCOVERY_SEARCH_FRESHNESS", "Month")
    importlib.reload(main)
    try:
        assert main.DISCOVERY_SEARCH_FRESHNESS == "Month"
    finally:
        # Restore a clean state for other test modules.
        monkeypatch.delenv("DISCOVERY_SEARCH_FRESHNESS", raising=False)
        importlib.reload(main)


def test_module_reload_invalid_env_falls_back(monkeypatch, capsys):
    """Illegal env value at import time must warn + fallback, not crash."""
    monkeypatch.setenv("DISCOVERY_SEARCH_FRESHNESS", "Bogus")
    importlib.reload(main)
    try:
        assert main.DISCOVERY_SEARCH_FRESHNESS == "Week"
        err = capsys.readouterr().err
        assert "WARN" in err and "Week" in err
    finally:
        monkeypatch.delenv("DISCOVERY_SEARCH_FRESHNESS", raising=False)
        importlib.reload(main)
