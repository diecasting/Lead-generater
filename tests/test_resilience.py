"""Tests for Phase 12.3: AI retry/fallback, history dedup, and HTML report.

All tests run with zero external dependencies (no network, no OpenAI key).
"""

import main


def test_is_retryable_429():
    e = Exception("rate limit")
    e.status = 429
    assert main._is_retryable(e) is True


def test_is_retryable_401_not_retried():
    e = Exception("unauthorized")
    e.status = 401
    assert main._is_retryable(e) is False


def test_is_retryable_timeout():
    e = Exception("ReadTimeout: connection timed out")
    assert main._is_retryable(e) is True


def test_dedupe_filters_seen():
    leads = [
        {"company": "A", "source_url": "http://a.com", "confidence": "high"},
        {"company": "B", "source_url": "http://b.com", "confidence": "medium"},
    ]
    kept = main.dedupe_leads(leads, {"http://a.com"})
    assert len(kept) == 1
    assert kept[0]["company"] == "B"


def test_history_roundtrip(tmp_path):
    p = str(tmp_path / "sent_cache.json")
    main.save_sent_history(["http://x.com/1", "http://x.com/2"], path=p)
    loaded = main.load_sent_history(path=p)
    assert loaded == {"http://x.com/1", "http://x.com/2"}


def test_load_history_missing_returns_empty(tmp_path):
    loaded = main.load_sent_history(path=str(tmp_path / "nope.json"))
    assert loaded == set()


def test_build_html_has_intent_and_link():
    leads = [{
        "company": "Acme Corp",
        "need_summary": "looking for aluminum die casting RFQ",
        "source_url": "http://acme.com/rfq",
        "keyword": "die casting buyer request for quote",
        "confidence": "high",
    }]
    html = main.build_html_report(leads, "2026-08-11 08:00 (GMT+8)")
    # 报告应展示意向等级标签（🔥/⚡/💤 之一）与可点击来源链接
    assert any(t in html for t in ("🔥 高意向", "⚡ 中意向", "💤 低意向"))
    assert "意向分" in html
    assert 'href="http://acme.com/rfq"' in html
    assert "Acme Corp" in html


def test_clean_with_ai_retries_then_falls_back(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    class FakeCompletions:
        def create(self, **kwargs):
            e = Exception("429 Too Many Requests")
            e.status = 429
            raise e

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *a, **k):
            self.chat = FakeChat()

    monkeypatch.setattr(main, "OpenAI", lambda *a, **k: FakeClient())

    raw = [{
        "title": "Buyer wants die casting",
        "snippet": "request for quote aluminum casting",
        "keyword": "die casting buyer request for quote",
        "url": "http://x.com/1",
    }]
    leads = main.clean_with_ai(raw)
    # 重试耗尽后应回退到规则清洗并仍返回合格线索（不丢线索）
    assert any(l["source_url"] == "http://x.com/1" for l in leads)
