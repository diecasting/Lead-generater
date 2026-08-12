"""Tests for email extraction, email graylist filtering, and keyword matrix."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main


def test_is_real_email_valid():
    assert main._is_real_email("sales@acme.com") is True
    assert main._is_real_email("John.Doe+rfq@factory-co.cn") is True


def test_is_real_email_rejects_placeholder_and_static():
    assert main._is_real_email("info@example.com") is False
    assert main._is_real_email("logo@site.png") is False
    assert main._is_real_email("noreply@acme.com") is False
    assert main._is_real_email("no-reply@acme.com") is False
    assert main._is_real_email("bad") is False
    assert main._is_real_email("@") is False


def test_extract_emails_from_text_filters_junk():
    text = (
        "Contact us at sales@acme.com or visit image@site.png. "
        "Auto mailer-daemon@acme.com bounce. RFQ to buyer@example.com. "
        "Reach John at john.doe@factory-co.cn for quote."
    )
    emails = main.extract_emails_from_text(text)
    assert "sales@acme.com" in emails
    assert "john.doe@factory-co.cn" in emails
    assert "image@site.png" not in emails
    assert "buyer@example.com" not in emails
    assert "mailer-daemon@acme.com" not in emails


def test_extract_emails_from_html_mailto_and_text():
    html = (
        "<html><body><p>Email sales@acme.com today.</p>"
        '<a href="mailto:info@widgets.io?subject=RFQ">write us</a>'
        '<img src="x@y.png"></body></html>'
    )
    emails = main.extract_emails_from_html(html)
    assert "sales@acme.com" in emails
    assert "info@widgets.io" in emails
    assert all(not e.endswith(".png") for e in emails)


def test_get_search_keywords_expanded_and_deduped():
    kws = main.get_search_keywords()
    # 4 个买方视角分组共 40 个关键词（已全部改写为买家/采购方措辞）
    assert len(kws) >= 30
    assert "aluminum die casting RFQ buyer" in kws
    assert "CNC machining RFQ buyer" in kws
    assert "OEM ODM inquiry custom parts" in kws
    assert len(kws) == len(set(kws)), "keywords must be de-duplicated"


def test_get_search_keywords_with_combine(monkeypatch):
    monkeypatch.setenv("SEARCH_COMBINE", "1")
    monkeypatch.setenv("SEARCH_COMBINE_MAX", "4")
    kws = main.get_search_keywords()
    assert any("RFQ" in k and "die casting" in k for k in kws)
    # combos capped at SEARCH_COMBINE_MAX
    assert len(kws) >= 30


def test_enrich_leads_with_emails_from_summary_only(monkeypatch):
    # disable network fetch to keep the test hermetic
    monkeypatch.setenv("EMAIL_EXTRACTION", "0")
    leads = [{
        "company": "Acme",
        "need_summary": "We need CNC parts, contact sales@acme.com",
        "source_url": "https://acme.com/rfq",
        "keyword": "CNC machining parts RFQ",
        "confidence": "high",
    }]
    out = main.enrich_leads_with_emails(leads)
    assert out[0]["emails"] == ["sales@acme.com"]


def test_enrich_leads_with_emails_no_email(monkeypatch):
    monkeypatch.setenv("EMAIL_EXTRACTION", "0")
    leads = [{
        "company": "X",
        "need_summary": "looking for a supplier",
        "source_url": "https://x.com",
        "keyword": "CNC machining parts RFQ",
        "confidence": "low",
    }]
    out = main.enrich_leads_with_emails(leads)
    assert out[0]["emails"] == []
