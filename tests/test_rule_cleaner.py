"""Tests for the deterministic rule-based lead cleaner (Phase 12.2 fallback).

These must run with zero external dependencies (no network, no OpenAI key),
so the daily job still produces leads when the API is unavailable.
"""

import main


def _raw(title, snippet, keyword, url="http://example.com/1"):
    return {"title": title, "snippet": snippet, "keyword": keyword, "url": url}


def test_rule_keeps_buying_intent():
    raw = [_raw(
        "Buyer wants aluminum die casting",
        "request for quote on custom casting parts",
        "die casting buyer request for quote",
    )]
    leads = main.clean_with_rules(raw)
    assert len(leads) == 1
    assert leads[0]["source_url"] == "http://example.com/1"
    assert leads[0]["confidence"] in {"high", "medium", "low"}


def test_rule_filters_supplier_self_ad():
    raw = [_raw(
        "We are a leading supplier of CNC parts",
        "Our company provides custom machining services. Contact us.",
        "CNC machining RFQ",
    )]
    leads = main.clean_with_rules(raw)
    assert leads == []  # supplier self-advertising is suppressed


def test_rule_filters_news_noise():
    raw = [_raw(
        "What is CNC machining? A tutorial",
        "This article explains how CNC works. Wiki definition inside.",
        "CNC machining RFQ",
    )]
    leads = main.clean_with_rules(raw)
    assert leads == []  # pure noise (tutorial / article) is dropped


def test_rule_dedup_by_url():
    raw = [
        _raw("X needs quote", "looking for supplier", "plastic mold RFQ", "http://c.com/1"),
        _raw("X duplicate", "looking for supplier", "plastic mold RFQ", "http://c.com/1"),
    ]
    leads = main.clean_with_rules(raw)
    assert len(leads) == 1


def test_rule_scores_and_sorts():
    raw = [
        _raw("Low intent buyer", "we need a quote", "plastic mold RFQ", "http://a.com/1"),
        _raw("Strong RFQ buyer", "request for quote aluminum die casting inquiry",
             "aluminum die casting inquiry", "http://b.com/1"),
    ]
    leads = main.clean_with_rules(raw)
    assert len(leads) == 2
    # higher-scoring item should come first
    assert "Strong" in leads[0]["company"] or "Strong" in leads[0]["need_summary"]


def test_clean_with_ai_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    raw = [_raw(
        "Buyer wants die casting",
        "request for quote aluminum casting",
        "die casting buyer request for quote",
    )]
    leads = main.clean_with_ai(raw)
    assert any(l["source_url"] == "http://example.com/1" for l in leads)
