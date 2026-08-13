"""Phase A1 — TTL lifecycle dedup tests.

Replaces the old permanent dedup (flat URL set in sent_cache.json) with a
TTL-aware cache ({key: {first_seen, last_seen}}). These tests pin the TTL
behavior, backward compatibility, expiration cleanup, and size bound.

All tests run with zero external dependencies (no network, no OpenAI key).
"""

import json
import time

import main

DAY = 86400


def _entry(last_seen_days_ago, first_seen_days_ago=0):
    now = time.time()
    return {
        "first_seen": now - first_seen_days_ago * DAY,
        "last_seen": now - last_seen_days_ago * DAY,
    }


# ---------------------------------------------------------------------------
# Test 1 — Fresh entry (last_seen == now) must be deduped
# ---------------------------------------------------------------------------
def test_fresh_entry_deduped():
    now = time.time()
    cache = {"http://a.com": {"first_seen": now, "last_seen": now}}
    kept = main.dedupe_leads([{"source_url": "http://a.com"}],
                             cache, ttl_days=30, now=now)
    assert len(kept) == 0


# ---------------------------------------------------------------------------
# Test 2 — Entry within TTL (10d < 30d) must be deduped
# ---------------------------------------------------------------------------
def test_within_ttl_deduped():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=10)}
    kept = main.dedupe_leads([{"source_url": "http://a.com"}],
                             cache, ttl_days=30, now=now)
    assert len(kept) == 0


# ---------------------------------------------------------------------------
# Test 3 — Entry at TTL boundary (age == TTL) -> expired -> allow rediscovery
# Boundary rule: age >= TTL => expired.
# ---------------------------------------------------------------------------
def test_ttl_boundary_expired():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=30)}
    kept = main.dedupe_leads([{"source_url": "http://a.com"}],
                             cache, ttl_days=30, now=now)
    assert len(kept) == 1  # allowed to re-enter pipeline


# ---------------------------------------------------------------------------
# Test 4 — Expired entry (31d > 30d) must NOT be deduped, can re-enter
# ---------------------------------------------------------------------------
def test_expired_entry_rediscoverable():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=31)}
    kept = main.dedupe_leads([{"source_url": "http://a.com"}],
                             cache, ttl_days=30, now=now)
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Test 5 — Expired entries are cleaned out of the active cache
# ---------------------------------------------------------------------------
def test_expired_entry_cleanup():
    now = time.time()
    entries = {
        "fresh": _entry(last_seen_days_ago=1),
        "expired": _entry(last_seen_days_ago=40),
    }
    pruned = main.prune_expired_entries(entries, ttl_days=30, now=now)
    assert "fresh" in pruned
    assert "expired" not in pruned


# ---------------------------------------------------------------------------
# Test 6 — Different lead keys do not affect each other
# ---------------------------------------------------------------------------
def test_different_keys_independent():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=0)}
    leads = [
        {"source_url": "http://a.com"},
        {"source_url": "http://b.com"},
    ]
    kept = main.dedupe_leads(leads, cache, ttl_days=30, now=now)
    assert len(kept) == 1
    assert kept[0]["source_url"] == "http://b.com"


# ---------------------------------------------------------------------------
# Test 7 — Same lead key repeated within TTL must be fully deduped
# ---------------------------------------------------------------------------
def test_same_key_within_ttl_deduped():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=5)}
    leads = [
        {"source_url": "http://a.com"},
        {"source_url": "http://a.com"},
    ]
    kept = main.dedupe_leads(leads, cache, ttl_days=30, now=now)
    assert len(kept) == 0


# ---------------------------------------------------------------------------
# Test 8 — Old cache format (legacy list) must be loadable, no crash
# ---------------------------------------------------------------------------
def test_legacy_list_format_loadable(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(["http://a.com", "http://b.com"], f)
    loaded = main.load_sent_history(path=p)
    assert set(loaded.keys()) == {"http://a.com", "http://b.com"}
    # migrated entries get a last_seen timestamp
    for v in loaded.values():
        assert "last_seen" in v


def test_legacy_urls_dict_format_loadable(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"urls": ["http://a.com"]}, f)
    loaded = main.load_sent_history(path=p)
    assert set(loaded.keys()) == {"http://a.com"}


# ---------------------------------------------------------------------------
# Test 9 — Invalid / corrupt cache entries must not crash the run
# ---------------------------------------------------------------------------
def test_corrupt_cache_no_crash(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ")
    loaded = main.load_sent_history(path=p)
    assert loaded == {}


def test_invalid_entry_in_new_format_ignored(tmp_path):
    now = time.time()
    p = str(tmp_path / "sent_cache.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "entries": {
            "http://a.com": {"first_seen": now, "last_seen": now},
            "bad": "not a dict",
            "worse": {"no_last_seen": 5},
        }}, f)
    loaded = main.load_sent_history(path=p)
    assert set(loaded.keys()) == {"http://a.com"}


# ---------------------------------------------------------------------------
# Test 10 — HISTORY_MAX still bounds cache size
# ---------------------------------------------------------------------------
def test_history_max_bounds_size(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    now = time.time()
    urls = [f"http://x.com/{i}" for i in range(10)]
    main.save_sent_history(urls, path=p, max_len=5, now=now)
    loaded = main.load_sent_history(path=p)
    assert len(loaded) == 5


# ---------------------------------------------------------------------------
# Supporting — default TTL is 30 days
# ---------------------------------------------------------------------------
def test_default_ttl_is_30():
    assert main.DISCOVERY_DEDUP_TTL_DAYS == 30


# ---------------------------------------------------------------------------
# Supporting — save persists new-format and survives reload
# ---------------------------------------------------------------------------
def test_ttl_roundtrip_persistence(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    now = time.time()
    main.save_sent_history(["http://a.com"], path=p, now=now)
    loaded = main.load_sent_history(path=p)
    assert "http://a.com" in loaded
    assert loaded["http://a.com"]["last_seen"] == now


# ---------------------------------------------------------------------------
# Supporting — save refreshes last_seen but keeps first_seen
# ---------------------------------------------------------------------------
def test_save_refreshes_last_seen_keeps_first_seen(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    now = time.time()
    main.save_sent_history(["http://a.com"], path=p, now=now - 10 * DAY)
    main.save_sent_history(["http://a.com"], path=p, now=now)
    loaded = main.load_sent_history(path=p)
    assert loaded["http://a.com"]["first_seen"] == now - 10 * DAY
    assert loaded["http://a.com"]["last_seen"] == now


# ---------------------------------------------------------------------------
# Supporting — leads without a URL are never deduped (preserves original rule)
# ---------------------------------------------------------------------------
def test_lead_without_url_never_deduped():
    now = time.time()
    cache = {"http://a.com": _entry(last_seen_days_ago=0)}
    leads = [{"company": "NoURL"}]
    kept = main.dedupe_leads(leads, cache, ttl_days=30, now=now)
    assert len(kept) == 1
