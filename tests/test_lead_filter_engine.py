"""lead_filter_engine 模块的单元测试 —— 反同行 + 买方闸门核心能力。"""
import os
import sys

import pytest

# 让测试能直接 import 仓库根目录下的独立模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lead_filter_engine as lfe


# ---------------------------------------------------------------------------
# 1) 同行邮箱识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("email,expected", [
    ("yongzhucasting@163.com", True),       # 前缀含 casting（强信号）
    ("sales@abc-machining.com", True),      # 域名含 machining（强信号）
    ("info@xyz-foundry.com", True),         # 域名含 foundry（强信号）
    ("acmetech@gmail.com", False),          # 弱信号 tech + 免费邮箱 -> 放行
    ("buyer@brandco.com", False),           # 普通买家企业邮箱 -> 放行
    ("hello@world.com", False),             # 无任何制造词 -> 放行
    ("not-an-email", False),                # 非法邮箱 -> 放行
    ("", False),
])
def test_is_competitor_email(email, expected):
    assert lfe.is_competitor_email(email) is expected


def test_competitor_email_weak_signal_on_corporate_domain():
    # 弱信号词（parts/manufacturer/tech…）仅在「非免费邮箱域名」时触发
    assert lfe.is_competitor_email("contact@bestparts.com") is True


# ---------------------------------------------------------------------------
# 2) 同行文本特征黑名单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    "we are a manufacturer",
    "our foundry",
    "casting capabilities",
    "machining services provider",
    "iso certified factory",
    "custom manufacturing solutions",
    "we specialize in manufacturing",
    "we manufacture aluminum parts",
    "request a quote from us",          # 供应商反向邀约
    "get a quote from us",
])
def test_is_competitor_text_phrases(kw):
    assert lfe.is_competitor(title=kw) is True


def test_is_competitor_regex_supplier_self_ad():
    # we/our + 供应商名词，中间不夹买方动词 -> 判同行
    assert lfe.is_competitor(snippet="We are a leading supplier of die casting") is True
    assert lfe.is_competitor(snippet="Our factory covers 20000 sqm") is True


def test_is_competitor_does_not_kill_real_buyer():
    # 真实买家 "we are looking for" 不应被同行正则误杀
    assert lfe.is_competitor(title="We are looking for a die casting supplier") is False


def test_filter_competitors_drops_self_ads():
    raw = [
        {"url": "https://acme-cast.com", "title": "We are a manufacturer of die casting",
         "snippet": "Our foundry offers ISO certified casting", "keyword": "looking for die casting supplier"},
        {"url": "https://buyer-co.com/rfq", "title": "We are looking for a die casting supplier",
         "snippet": "Our company needs custom aluminum parts", "keyword": "looking for die casting supplier"},
    ]
    kept = lfe.filter_competitors(raw)
    assert len(kept) == 1
    assert kept[0]["url"] == "https://buyer-co.com/rfq"


# ---------------------------------------------------------------------------
# 3) 严格买方意图 + 负向预查闸门
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("We are looking for a die casting supplier", True),
    ("Our company needs custom CNC parts, requesting quotes", True),
    ("Seeking a manufacturer for our new product", True),
    ("We need to outsource metal casting production", True),
    ("RFQ #2026-883 need custom aluminum bracket", True),     # 求购贴 RFQ 编号
    ("Looking for CAD drawing of enclosure", True),            # 图纸需求
    ("Request a quote from us for your project", False),       # 反向邀约 -> 排除
    ("We are a manufacturer of precision parts", False),       # 同行自广告
    ("How to choose a CNC machine", False),                    # 无关文章
])
def test_is_true_buyer(text, expected):
    assert lfe.is_true_buyer(text) is expected


def test_passes_buyer_gate_aliases_is_true_buyer():
    assert lfe.passes_buyer_gate("looking to source plastic molding") is True
    assert lfe.passes_buyer_gate("request a quote from us") is False


# ---------------------------------------------------------------------------
# 4) 邮箱提取后处理 + 一键流水线
# ---------------------------------------------------------------------------
def test_filter_competitor_emails_strips_and_drops():
    leads = [
        # 全部是同行邮箱 + 非买方正文 -> 整页丢弃
        {"emails": ["yongzhucasting@163.com", "sales@abc-machining.com"],
         "need_summary": "we are a manufacturer", "keyword": "", "source_url": "https://x.com"},
        # 同行邮箱 + 买方正文 -> 仅剥离同行邮箱，保留线索
        {"emails": ["sales@abc-machining.com", "buyer@brandco.com"],
         "need_summary": "we are looking for a supplier", "keyword": "", "source_url": "https://y.com"},
        # 正常买家邮箱 -> 全部保留
        {"emails": ["buyer@brandco.com"],
         "need_summary": "requesting quotes", "keyword": "", "source_url": "https://z.com"},
    ]
    kept = lfe.filter_competitor_emails(leads)
    assert len(kept) == 2
    # 第二条：同行邮箱被剥离，仅留买家邮箱
    mid = next(l for l in kept if "y.com" in l["source_url"])
    assert mid["emails"] == ["buyer@brandco.com"]


def test_filter_leads_pipeline():
    raw = [
        {"url": "https://acme-cast.com", "title": "We are a manufacturer",
         "snippet": "our foundry", "keyword": "looking for die casting supplier"},
        {"url": "https://buyer-co.com", "title": "We are looking for a die casting supplier",
         "snippet": "our company needs parts", "keyword": "looking for die casting supplier"},
        {"url": "https://rfq-site.com", "title": "RFQ for aluminum bracket",
         "snippet": "requesting quotation", "keyword": "seeking CNC machining supplier"},
    ]
    survivors = lfe.filter_leads(raw)
    urls = {r["url"] for r in survivors}
    assert "https://acme-cast.com" not in urls       # 同行已剔除
    assert "https://buyer-co.com" in urls            # 真实买家保留
    assert "https://rfq-site.com" in urls            # 求购贴保留
