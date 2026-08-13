"""Phase B — Discovery Lead Quality Hardening 测试套件。

覆盖：
  B1 通用 Supplier / Manufacturer 标题识别（组合语义，不误杀买家）
  B2 AI 清洗后强制再跑确定性闸门（competitor gate + buyer gate）
  B3 7 类公司角色分类（与 is_competitor / is_true_buyer 解耦）
  B4 供应商目录 Listing 过滤（目录站 + 无买方意图才剔除；有意图放行）
不改动 score_lead / threshold / 搜索量 / freshness / cache。
"""
import main
import lead_filter_engine as lfe


# ---------------------------------------------------------------------------
# B1 — 通用 Supplier / Manufacturer 标题识别
# ---------------------------------------------------------------------------
def test_generic_supplier_title_aluminum_services():
    # "Aluminum Die Casting Services" 必须被识别为同行/供应商
    assert lfe.is_competitor(title="Aluminum Die Casting Services") is True
    assert main.is_competitor("", "Aluminum Die Casting Services", "") is True


def test_generic_supplier_title_die_casting_supplier():
    assert lfe.is_competitor(title="Die Casting Supplier") is True


def test_generic_manufacturer_title_casting():
    # "Casting Manufacturer" 必须被识别为 supplier/competitor 类型，而非 buyer
    assert lfe.is_competitor(title="Casting Manufacturer") is True


def test_generic_supplier_cnc_machining_manufacturer():
    # 用户列出的 CNC 类供应商标题
    assert lfe.is_competitor(title="CNC Machining Manufacturer") is True
    assert lfe.is_competitor(title="Metal Casting Supplier") is True
    assert lfe.is_competitor(title="Injection Molding Supplier") is True


def test_existing_competitor_rule_preserved():
    # 旧规则 "we are a manufacturer" 仍然被识别（未删除）
    assert lfe.is_competitor(
        title="We are a manufacturer of aluminum die castings.") is True
    assert lfe.is_competitor(snippet="Our foundry offers ISO certified casting") is True


def test_competitor_title_does_not_kill_real_buyer():
    # 买家页面即使出现 "die casting supplier" 等词也不应被 is_competitor 误杀
    assert lfe.is_competitor(
        title="We are looking for an aluminum die casting supplier",
        snippet="Please submit your quotation and lead time") is False
    assert lfe.is_competitor(
        "", "Buyer seeking CNC machining supplier for outsourcing production",
        "request for quote on 5000 pcs") is False


# ---------------------------------------------------------------------------
# B2 — AI 清洗后强制再跑确定性闸门
# ---------------------------------------------------------------------------
def test_ai_false_positive_competitor_rejected():
    # 模拟：AI 标注 BUYER，但原始 need_summary 明显是供应商自广告
    leads = [{
        "company": "ACME Cast",
        "need_summary": "Aluminum Die Casting Supplier",
        "keyword": "looking for die casting supplier",
        "source_url": "https://acme-cast.com",
        "confidence": "high",
    }]
    kept, comp_drop, buyer_drop = main.apply_post_ai_gates(leads)
    assert comp_drop == 1
    assert buyer_drop == 0
    assert kept == []  # AI 无法绕过 competitor gate


def test_ai_false_positive_buyer_gate_rejected():
    # 模拟：AI 标注 BUYER，但缺乏必要买方意图（只参加了展会、见了供应商）
    leads = [{
        "company": "Some Co",
        "need_summary": "We attended the manufacturing expo and met many suppliers",
        "keyword": "seeking CNC machining supplier",
        "source_url": "https://example.com/post",
        "confidence": "high",
    }]
    kept, comp_drop, buyer_drop = main.apply_post_ai_gates(leads)
    assert comp_drop == 0
    assert buyer_drop == 1
    assert kept == []  # AI 无法绕过 buyer gate


def test_real_buyer_survives_post_ai_gates():
    # 真实买方（带 manufacturer/supplier 等普通词）必须存活并标记 BUYER
    leads = [{
        "company": "Brand Co",
        "need_summary": ("We are looking for an aluminum die casting supplier "
                         "for 20,000 pcs/year. Please submit your quotation "
                         "and lead time."),
        "keyword": "looking for die casting supplier",
        "source_url": "https://brandco.com/rfq",
        "confidence": "high",
    }]
    kept, comp_drop, buyer_drop = main.apply_post_ai_gates(leads)
    assert comp_drop == 0 and buyer_drop == 0
    assert len(kept) == 1
    assert kept[0].get("_company_class") == "BUYER"


def test_post_ai_gate_idempotent_on_rule_fallback():
    # clean_with_ai 在失败时回退 clean_with_rules，其产出已通过闸门；
    # 再跑一次 post-ai gate 不应误杀真实买方。
    raw = [{
        "url": "https://buyer-co.com/rfq",
        "title": "We are looking for a die casting supplier",
        "snippet": "Our company needs custom aluminum parts, requesting quotes",
        "keyword": "looking for die casting supplier",
    }]
    ruled = main.clean_with_rules(raw)
    assert len(ruled) == 1
    kept, _, _ = main.apply_post_ai_gates(ruled)
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# B3 — 7 类公司角色分类
# ---------------------------------------------------------------------------
def test_seven_class_classification():
    cases = {
        "BUYER": {"need_summary": "We are looking for a die casting supplier",
                  "keyword": "looking for die casting supplier"},
        "SUPPLIER": {"need_summary": "We supply custom metal brackets to automotive clients",
                     "keyword": ""},
        "COMPETITOR": {"need_summary": "Aluminum Die Casting Services",
                       "keyword": "", "source_url": "https://acme-cast.com"},
        "OEM": {"need_summary": "OEM is looking for die casting suppliers, submit RFQ for 50000 parts",
                "keyword": "oem buyer"},
        "DISTRIBUTOR": {"need_summary": "We are a distributor of industrial fasteners",
                        "keyword": ""},
        "SERVICE_PROVIDER": {"need_summary": "Engineering design service provider offering prototyping",
                             "keyword": ""},
        "IRRELEVANT": {"need_summary": "Latest news about aluminum market prices and industry trends in 2026",
                       "keyword": ""},
    }
    for expected, lead in cases.items():
        got = lfe.classify_company(lead)
        assert got == expected, f"expected {expected}, got {got} for {lead['need_summary']!r}"
    # 全部 7 类均可合法返回
    assert set(cases.keys()) == set(lfe.COMPANY_CLASSES)


# ---------------------------------------------------------------------------
# B4 — 供应商目录 Listing 过滤
# ---------------------------------------------------------------------------
def test_directory_listing_without_buyer_intent_rejected():
    # 目录站纯 Listing，无 RFQ / 寻源 / 采购信号 -> 剔除
    url = "https://www.thomasnet.com/profile/xyz"
    assert main.is_directory_listing_lead(
        url, "Company XYZ Aluminum Die Casting Supplier",
        "Supplier Directory Products: brackets, housings") is True
    raw = [{
        "url": url,
        "title": "Company XYZ Aluminum Die Casting Supplier",
        "snippet": "Supplier Directory Products: brackets, housings",
        "keyword": "looking for die casting supplier",
    }]
    kept = main.filter_directory_listings(raw)
    assert kept == []


def test_directory_listing_with_buyer_intent_kept():
    # 目录站页面带有真实买方意图 -> 不得仅因域名规则直接误杀
    url = "https://www.thomasnet.com/rfq/abc"
    assert main.is_directory_listing_lead(
        url, "Automotive OEM is looking for aluminum die casting suppliers",
        "Submit RFQ for 50,000 parts/year") is False
    raw = [{
        "url": url,
        "title": "Automotive OEM is looking for aluminum die casting suppliers",
        "snippet": "Submit RFQ for 50,000 parts/year",
        "keyword": "looking for die casting supplier",
    }]
    kept = main.filter_directory_listings(raw)
    assert len(kept) == 1


def test_non_directory_domain_not_affectedly_filtered():
    # 非目录域名不应被目录 Listing 规则影响
    assert main.is_directory_listing_lead(
        "https://brandco.com/rfq", "We are looking for a die casting supplier",
        "request for quote") is False
