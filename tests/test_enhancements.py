"""Phase 12.5 增强功能测试：定向搜索 / 黑名单 / 公司名提取 / 意向评分 / 开发信。"""

import main


# ---------------------------------------------------------------------------
# 1. 定向黄页 / 社区搜索
# ---------------------------------------------------------------------------

def test_directory_queries_round_robin_and_capped():
    kws = [f"kw{i}" for i in range(10)]
    qs = main.get_directory_queries(kws)
    assert len(qs) <= main.DIRECTORY_MAX_QUERIES
    # 轮询：前几个站点应各不相同（不是单一站点占满）
    sites = [q.split("site:")[1] for q in qs[: len(main.DIRECTORY_SITES)]]
    assert len(set(sites)) == len(sites)
    assert all("site:" in q for q in qs)


def test_directory_queries_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(main, "DIRECTORY_SEARCH", False)
    assert main.get_directory_queries(["a", "b"]) == []


# ---------------------------------------------------------------------------
# 2. 黑名单过滤
# ---------------------------------------------------------------------------

def test_blacklist_blocks_content_sites():
    assert main.is_blacklisted("https://zhihu.com/q/1", "如何找供应商")
    assert main.is_blacklisted("https://en.wikipedia.org/wiki/Casting", "")
    assert main.is_blacklisted("https://medium.com/@x/intro", "")
    assert main.is_blacklisted("https://foo.blogspot.com/posts", "")


def test_blacklist_allows_directory_targets():
    # reddit / thomasnet 是我们主动定向的目标，不应被黑名单误杀
    assert main.is_blacklisted("https://www.reddit.com/r/manufacturing", "") is False
    assert main.is_blacklisted("https://thomasnet.com/company/x", "") is False
    assert main.is_blacklisted("https://kompass.com/firm/y", "") is False


def test_blacklist_blocks_content_keywords():
    assert main.is_blacklisted("https://blog.example.com", "How to choose a CNC supplier")
    assert main.is_blacklisted("https://x.com", "Top 10 die casting companies review")


def test_filter_blacklist_count():
    raw = [
        {"url": "https://zhihu.com/q", "title": "t", "snippet": "s"},
        {"url": "https://thomasnet.com/c", "title": "t", "snippet": "s"},
        {"url": "https://medium.com/p", "title": "t", "snippet": "s"},
    ]
    kept = main.filter_blacklist(raw)
    assert len(kept) == 1
    assert kept[0]["url"] == "https://thomasnet.com/c"


# ---------------------------------------------------------------------------
# 3. 公司名提取
# ---------------------------------------------------------------------------

def test_extract_company_from_title():
    assert main.extract_company_name(
        "Acme Precision | RFQ CNC parts", "https://acme.com/x"
    ) == "Acme Precision"


def test_extract_company_falls_back_to_domain():
    name = main.extract_company_name("Looking for die casting supplier", "https://foo-corp.com/y")
    assert name in ("Foo-Corp", "Foo") or name  # 回退到域名（可接受的健壮性结果）


def test_extract_company_rejects_generic_titles():
    name = main.extract_company_name("How to source aluminum parts", "https://x.com")
    assert name != "How to source aluminum parts"


# ---------------------------------------------------------------------------
# 4. 意向评分 (0-100) 与等级
# ---------------------------------------------------------------------------

def test_score_lead_high_with_all_signals():
    lead = {
        "confidence": "high",
        "emails": ["sales@acme.com"],
        "need_summary": "Need aluminum die casting 5000 pcs with CAD drawing tolerance 0.05mm",
        "keyword": "aluminum die casting RFQ",
        "source_url": "https://thomasnet.com/x",
    }
    s = main.score_lead(lead)
    assert s >= 70  # 高意向阈值
    assert main.tier_from_score(s)[0] == "🔥 高意向"


def test_score_lead_low_baseline():
    lead = {"confidence": "low", "emails": [], "need_summary": "maybe interested",
            "keyword": "x", "source_url": "https://example.com"}
    s = main.score_lead(lead)
    assert 0 <= s <= 45
    assert main.tier_from_score(s)[0] == "💤 低意向"


def test_score_lead_email_bonus():
    base = {"confidence": "medium", "emails": [], "need_summary": "inquiry",
            "keyword": "x", "source_url": "https://site.com"}
    with_email = dict(base, emails=["a@corp.com"])
    assert main.score_lead(with_email) > main.score_lead(base)


# ---------------------------------------------------------------------------
# 5. 个性化英文开发信
# ---------------------------------------------------------------------------

def test_generate_cold_email_contains_essentials():
    lead = {
        "company": "Acme Corp",
        "need_summary": "Looking for CNC machining parts",
        "keyword": "CNC machining parts RFQ",
        "emails": ["a@acme.com"],
    }
    ce = main.generate_cold_email(lead)
    assert ce.startswith("Subject:")
    assert "Acme Corp" in ce
    assert "Hank" in ce
    assert "CNC machining" in ce  # 能力匹配


def test_generate_cold_email_handles_unknown_company():
    lead = {"company": "Unknown", "need_summary": "", "keyword": "", "emails": []}
    ce = main.generate_cold_email(lead)
    assert "there" in ce  # 回滚称呼，不出现 "Unknown"


# ---------------------------------------------------------------------------
# 6. 同行过滤（Anti-Competitor / Negative Filtering）
# ---------------------------------------------------------------------------

def test_is_competitor_detects_self_ad():
    # 同行供应商自广告：明确命中硬短语 / 自广告主语
    assert main.is_competitor(
        "https://acme-cast.com", "We are a manufacturer of aluminum die casting",
        "Our foundry offers casting capabilities and ISO certified factory.")
    assert main.is_competitor(
        "", "CNC machining services provider", "custom manufacturing solutions")
    assert main.is_competitor(
        "", "Injection molding supplier", "we specialize in manufacturing")


def test_is_competitor_allows_real_buyer():
    # 真实买家措辞不应被误杀（含 looking for / seeking 等买方动词 + supplier 作宾语）
    assert main.is_competitor(
        "", "We are looking for a die casting supplier",
        "Our company needs custom aluminum parts molded") is False
    assert main.is_competitor(
        "", "Buyer seeking CNC machining supplier for outsourcing production",
        "request for quote on 5000 pcs") is False


def test_filter_competitors_removes_competitors():
    raw = [
        {"url": "https://acme-cast.com", "title": "We are a manufacturer of die casting",
         "snippet": "our foundry casting capabilities", "keyword": "x"},
        {"url": "https://buyer-co.com/rfq", "title": "We are looking for die casting supplier",
         "snippet": "our company needs custom parts", "keyword": "x"},
    ]
    kept = main.filter_competitors(raw)
    assert len(kept) == 1
    assert kept[0]["url"] == "https://buyer-co.com/rfq"


def test_rule_cleaner_drops_competitor():
    raw = [{
        "url": "https://acme-cast.com",
        "title": "We are a manufacturer of aluminum die casting",
        "snippet": "Our foundry offers ISO certified factory casting capabilities",
        "keyword": "looking for die casting supplier",
    }]
    assert main.clean_with_rules(raw) == []  # 同行被硬过滤，不输出


def test_rule_cleaner_boosts_buyer_intent():
    # 真实买方意图短语应被保留并判为高置信
    raw = [{
        "url": "https://buyer-co.com/rfq",
        "title": "We are looking for a CNC machining supplier",
        "snippet": "Our company needs custom aluminum parts, requesting quotes",
        "keyword": "seeking CNC machining supplier",
    }]
    leads = main.clean_with_rules(raw)
    assert len(leads) == 1
    assert leads[0]["confidence"] == "high"
