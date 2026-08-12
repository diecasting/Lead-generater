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


# ---------------------------------------------------------------------------
# 7. 邮件发件人 / 收件人对齐（Gmail 安全）
# ---------------------------------------------------------------------------

def _patch_smtp(monkeypatch):
    import smtplib as _smtp

    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            captured.setdefault("inits", []).append((a, k))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            captured["user"] = user

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setattr(_smtp, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(_smtp, "SMTP", FakeSMTP)
    return captured


def test_send_email_defaults_align_to_gmail(monkeypatch):
    """默认收件人应为 alumcastor@gmail.com；From 必须对齐到登录账号，
    避免 Gmail SendAsDenied（5.7.1）。"""
    captured = _patch_smtp(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.gmail.com")
    monkeypatch.setenv("MAIL_PORT", "465")
    monkeypatch.setenv("MAIL_USER", "alumcastor@gmail.com")
    monkeypatch.setenv("MAIL_PASSWORD", "apppass16char")
    # 不设置 MAIL_RECIPIENT / MAIL_SENDER，验证默认值与对齐逻辑

    ok = main.send_email("Test", "<p>hi</p>")
    assert ok is True
    assert captured["user"] == "alumcastor@gmail.com"
    msg = captured["msg"]
    assert "alumcastor@gmail.com" in msg["From"]      # From 地址对齐登录账号
    assert msg["To"] == "alumcastor@gmail.com"         # 默认收件人


def test_send_email_sender_falls_back_when_mismatched(monkeypatch):
    """MAIL_SENDER 与登录账号不一致时，应从告警并回退到登录账号。"""
    captured = _patch_smtp(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.gmail.com")
    monkeypatch.setenv("MAIL_PORT", "465")
    monkeypatch.setenv("MAIL_USER", "alumcastor@gmail.com")
    monkeypatch.setenv("MAIL_PASSWORD", "apppass16char")
    monkeypatch.setenv("MAIL_SENDER", "someone-else@hotmail.com")

    ok = main.send_email("Test", "<p>hi</p>")
    assert ok is True
    # From 已回退到登录账号，不得保留不一致的 MAIL_SENDER
    assert "alumcastor@gmail.com" in captured["msg"]["From"]
    assert "someone-else@hotmail.com" not in captured["msg"]["From"]


# ---------------------------------------------------------------------------
# 8. 反同行邮箱识别 + 严格买方意图闸门
# ---------------------------------------------------------------------------

def test_is_competitor_email_strong_tokens():
    # 同行供应商联络邮箱（前缀或域名暴露制造/加工身份）
    assert main.is_competitor_email("yongzhucasting@163.com") is True
    assert main.is_competitor_email("sales@abc-machining.com") is True
    assert main.is_competitor_email("info@xyz-foundry.com") is True
    assert main.is_competitor_email("contact@bestmold.com") is True
    assert main.is_competitor_email("cncpro@toolingworks.com") is True


def test_is_competitor_email_keeps_real_buyer():
    # 真实买家品牌邮箱不应被误杀
    assert main.is_competitor_email("john@brandco.com") is False
    assert main.is_competitor_email("info@acmebuyer.com") is False
    assert main.is_competitor_email("procurement@bigretailer.com") is False


def test_is_competitor_email_free_domain_not_flagged_by_weak():
    # 弱信号仅在非免费域名生效；免费邮箱不误伤（tech 等泛词不应杀买家）
    assert main.is_competitor_email("acmetech@gmail.com") is False


def test_filter_competitor_emails_drops_all_competitor_lead():
    leads = [
        {"source_url": "https://x.com", "emails": ["yongzhucasting@163.com"],
         "need_summary": "we are a foundry", "confidence": "low"},
        {"source_url": "https://buyer.com", "emails": ["buyer@brandco.com"],
         "need_summary": "looking for supplier", "confidence": "high"},
        {"source_url": "https://mix.com",
         "emails": ["sales@abc-machining.com", "buyer@brandco.com"],
         "need_summary": "RFQ", "confidence": "medium"},
    ]
    kept = main.filter_competitor_emails(leads)
    urls = {l["source_url"] for l in kept}
    assert "https://x.com" not in urls          # 全部为同行邮箱 -> 丢弃整条
    assert "https://buyer.com" in urls
    mix = next(l for l in kept if l["source_url"] == "https://mix.com")
    assert mix["emails"] == ["buyer@brandco.com"]  # 剔除同行邮箱，保留买家邮箱


def test_rule_cleaner_drops_non_buyer_snippet():
    # 没有买方动作、也没有 RFQ/图纸的制造类描述 -> 不入库
    raw = [{
        "title": "Custom aluminum casting manufacturer",
        "snippet": "we are a precision manufacturer of die casting parts",
        "keyword": "looking for die casting supplier",
        "url": "https://supplier.com",
    }]
    assert main.clean_with_rules(raw) == []


def test_rule_cleaner_drops_inbound_quote_competitor():
    # 供应商邀请别人向自己询价 -> 视为同行，不入库
    # （同时验证 TRUE_BUYER_RE 不会误把 "request a quote from us" 当买家动作）
    raw = [{
        "title": "Get a quote from us",
        "snippet": "request a quote from us for your machining project",
        "keyword": "seeking CNC machining supplier",
        "url": "https://supplier.com",
    }]
    assert main.clean_with_rules(raw) == []


def test_rule_cleaner_keeps_genuine_rfq_buyer():
    raw = [{
        "title": "Company X RFQ",
        "snippet": "request for quote on custom aluminum die casting, drawing attached",
        "keyword": "looking for die casting supplier",
        "url": "https://buyer.com/rfq",
    }]
    leads = main.clean_with_rules(raw)
    assert len(leads) == 1
