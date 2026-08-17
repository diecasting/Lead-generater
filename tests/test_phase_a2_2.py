"""Phase A2.2-1 — Safe Discovery Experiment Infrastructure.

Scope of this phase (and ONLY this):
- Add DISCOVERY_DRY_RUN (default false -> production unchanged).
- Emit discovery_metrics.json (funnel metrics) on every run.
- In dry-run: still run search / filter / AI / enrichment / reports,
  but MUST NOT call send_email() (no SMTP) and MUST NOT call
  save_sent_history() (sent_cache stays untouched).

This phase does NOT touch any Discovery strategy: SEARCH_PER_KEYWORD,
RESULTS_LIMIT, keywords, pagination, deep crawl, freshness default, score,
score weights, thresholds, buyer/competitor gates, directory filtering,
AI prompts, dedup algorithm, TTL, HISTORY_MAX, Bing/DDG providers.
"""

import importlib
import json

import main


# ---------------------------------------------------------------------------
# 1) Config default + env parsing
# ---------------------------------------------------------------------------
def test_default_dry_run_false(monkeypatch):
    """Unconfigured -> production behavior (no dry-run)."""
    monkeypatch.delenv("DISCOVERY_DRY_RUN", raising=False)
    importlib.reload(main)
    assert main.DISCOVERY_DRY_RUN is False


def test_env_parsing(monkeypatch):
    """Truthy / falsy env values resolve deterministically; invalid -> false."""
    truthy = ("true", "1", "yes", "on", "TRUE", "True", "  yes  ")
    for v in truthy:
        monkeypatch.setenv("DISCOVERY_DRY_RUN", v)
        importlib.reload(main)
        assert main.DISCOVERY_DRY_RUN is True, v
    falsy = ("false", "0", "no", "off", "", "random", "2")
    for v in falsy:
        monkeypatch.setenv("DISCOVERY_DRY_RUN", v)
        importlib.reload(main)
        assert main.DISCOVERY_DRY_RUN is False, v
    # restore a clean default for other modules
    monkeypatch.delenv("DISCOVERY_DRY_RUN", raising=False)
    importlib.reload(main)
    assert main.DISCOVERY_DRY_RUN is False


# ---------------------------------------------------------------------------
# 2) Pipeline fakes — deterministic funnel with controlled drops
# ---------------------------------------------------------------------------
_RAW = [{"source_url": f"http://x.com/{i}"} for i in range(10)]


def _fake_collect():
    return [dict(r) for r in _RAW]


def _fake_blacklist(results):
    return results[2:]            # drop 2


def _fake_competitors(results):
    return results[1:]            # drop 1


def _fake_directory(results):
    return results[1:]            # drop 1


def _fake_clean(raw):
    return raw[2:]                # AI drops 2


def _fake_post_ai(leads):
    return leads[2:], 1, 1        # keep rest; comp_drop=1, buyer_drop=1


def _fake_enrich(leads):
    out = []
    for l in leads:
        l = dict(l)
        l["emails"] = ["buyer@acme.com"]
        out.append(l)
    return out


def _fake_competitor_emails(leads):
    return [dict(l) for l in leads]


def _fake_load_history(*a, **k):
    return {}


def _install_pipeline(monkeypatch, send_spy=None, save_spy=None):
    """Wire all external/heavy stages to deterministic fakes + spies."""
    monkeypatch.setattr(main, "validate_config", lambda: None)
    monkeypatch.setattr(main, "collect_raw_leads", _fake_collect)
    monkeypatch.setattr(main, "filter_blacklist", _fake_blacklist)
    monkeypatch.setattr(main, "filter_competitors", _fake_competitors)
    monkeypatch.setattr(main, "filter_directory_listings", _fake_directory)
    monkeypatch.setattr(main, "clean_with_ai", _fake_clean)
    monkeypatch.setattr(main, "apply_post_ai_gates", _fake_post_ai)
    monkeypatch.setattr(main, "enrich_leads_with_emails", _fake_enrich)
    monkeypatch.setattr(main, "filter_competitor_emails", _fake_competitor_emails)
    monkeypatch.setattr(main, "load_sent_history", _fake_load_history)
    if send_spy is not None:
        monkeypatch.setattr(main, "send_email", send_spy)
    if save_spy is not None:
        monkeypatch.setattr(main, "save_sent_history", save_spy)


def _read_metrics(tmp_path):
    with open(tmp_path / "discovery_metrics.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 3) DRY-RUN: no email, no history write, metrics emitted (sent=0)
# ---------------------------------------------------------------------------
def test_dry_run_skips_send_and_history(monkeypatch, tmp_path):
    send_calls = []
    save_calls = []

    def _send(subject, html):
        send_calls.append((subject, html))
        return True

    def _save(urls, *a, **k):
        save_calls.append(urls)

    _install_pipeline(monkeypatch, send_spy=_send, save_spy=_save)
    monkeypatch.setattr(main, "DISCOVERY_DRY_RUN", True)
    monkeypatch.chdir(tmp_path)

    main.main()

    # MUST NOT SEND EMAIL / TOUCH SMTP
    assert send_calls == [], "send_email must not be called in dry-run"
    # MUST NOT WRITE SENT HISTORY
    assert save_calls == [], "save_sent_history must not be called in dry-run"
    # Metrics emitted with dry_run flag + sent=0
    m = _read_metrics(tmp_path)
    assert m["dry_run"] is True
    assert m["funnel"]["sent"] == 0
    # Reports still produced
    assert (tmp_path / "leads_report.json").exists()
    assert (tmp_path / "leads_report.html").exists()


# ---------------------------------------------------------------------------
# 4) PRODUCTION: still sends + writes history, metrics emitted (sent=1)
# ---------------------------------------------------------------------------
def test_production_sends_and_writes_history(monkeypatch, tmp_path):
    send_calls = []
    save_calls = []

    def _send(subject, html):
        send_calls.append((subject, html))
        return True

    def _save(urls, *a, **k):
        save_calls.append(urls)

    _install_pipeline(monkeypatch, send_spy=_send, save_spy=_save)
    monkeypatch.setattr(main, "DISCOVERY_DRY_RUN", False)
    monkeypatch.chdir(tmp_path)

    main.main()

    assert len(send_calls) == 1, "production must send exactly one email"
    assert len(save_calls) == 1, "production must write sent history once"
    m = _read_metrics(tmp_path)
    assert m["dry_run"] is False
    assert m["funnel"]["sent"] == 1


# ---------------------------------------------------------------------------
# 5) Funnel math is correct (counts reflect each stage's drops)
# ---------------------------------------------------------------------------
def test_metrics_funnel_counts(monkeypatch, tmp_path):
    _install_pipeline(monkeypatch)
    monkeypatch.setattr(main, "DISCOVERY_DRY_RUN", False)
    monkeypatch.chdir(tmp_path)

    main.main()

    m = _read_metrics(tmp_path)["funnel"]
    # raw 10 -> blacklist-2 -> competitors-1 -> directory-1 -> 6 unique
    # -> AI-2 -> 4 -> post-ai(comp1,buyer1) -> 2 -> dedup0 -> 2 final
    assert m["raw_results"] == 10
    assert m["blacklist_rejected"] == 2
    assert m["competitor_rejected"] == 2          # 1 raw + 1 post-ai
    assert m["directory_rejected"] == 1
    assert m["unique_candidates"] == 6
    assert m["ai_rejected"] == 2
    assert m["buyer_gate_rejected"] == 1
    assert m["dedup_rejected"] == 0
    assert m["final_qualified"] == 2
    assert m["emails_extracted"] == 2


# ---------------------------------------------------------------------------
# 6) Metrics file is emitted in BOTH modes (additive, low-risk)
# ---------------------------------------------------------------------------
def test_metrics_emitted_in_dry_run_and_production(monkeypatch, tmp_path):
    for dry in (True, False):
        _install_pipeline(monkeypatch)
        monkeypatch.setattr(main, "DISCOVERY_DRY_RUN", dry)
        monkeypatch.chdir(tmp_path)
        main.main()
        assert (tmp_path / "discovery_metrics.json").exists()
        m = _read_metrics(tmp_path)
        assert set(m.keys()) >= {"generated_at", "dry_run", "freshness", "funnel"}
        assert set(m["funnel"].keys()) == {
            "raw_results", "unique_candidates", "blacklist_rejected",
            "competitor_rejected", "directory_rejected", "ai_rejected",
            "buyer_gate_rejected", "dedup_rejected", "final_qualified",
            "emails_extracted", "sent", "watch_recovered",
        }
