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
@pytest.mark.parametrize("kw,expected", [
    ("we are a manufacturer", True),
    ("our foundry", True),
    ("machining services provider", True),
    ("iso certified factory", True),
    ("we specialize in manufacturing", True),
    ("we manufacture aluminum parts", True),
    ("request a quote from us", True),          # 供应商反向邀约
    ("get a quote from us", True),
    # A2.2-5：以下泛化制造词汇已移出硬黑名单（不再仅凭能力 / 认证 / 设备词判定同行），
    # 避免误杀中立页面；它们现在应判为非同行（除非同时显式自称 Manufacturer/Factory/Foundry）
    ("casting capabilities", False),
    ("custom manufacturing solutions", False),
])
def test_is_competitor_text_phrases(kw, expected):
    assert lfe.is_competitor(title=kw) is expected


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
    # A2.2-5 放宽买方闸门：新增显式询价 / 寻源短语
    ("looking for quotation on 5000 pcs bracket", True),       # looking for quotation
    ("need pricing for custom aluminum housing", True),        # need pricing for
    ("requesting a quote for our new project", True),          # requesting a quote
    ("sourcing parts for our OEM production line", True),      # sourcing parts
    ("manufacturing partner needed for new product", True),    # manufacturing partner needed
    ("supplier required for die casting project", True),       # supplier required
    # A2.2-5 包容性增强：海外采购商常见询价 / 寻源表达
    ("please send us your quotation for the enclosure", True),  # please send us a quote
    ("send us your quotation for aluminum housing", True),      # send your quotation
    ("quote needed for 1000 pcs bracket", True),                # quote needed
    ("price inquiry for zinc die casting", True),               # price inquiry
    ("we are in the market for a CNC machining partner", True), # we are in the market for
    ("we require a die casting supplier", True),                # we require a supplier
    ("die casting supplier needed for our project", True),      # 买家求供（非同行自广告）
    ("CNC machining manufacturer wanted", True),                # 买家求供
    ("looking to buy custom injection molded parts", True),     # 明确购买意图
    ("we import from china and need a manufacturer", True),     # 海外采购信号
    ("overseas supplier needed for metal components", True),     # overseas supplier
    ("Request a quote from us for your project", False),       # 反向邀约 -> 排除
    ("We are a manufacturer of precision parts", False),       # 同行自广告
    ("How to choose a CNC machine", False),                    # 无关文章
])
def test_is_true_buyer(text, expected):
    assert lfe.is_true_buyer(text) is expected


def test_is_competitor_spares_supplier_needed_query():
    # A2.2-5 精准度：买家「求供」信号（supplier needed / manufacturer wanted）不应被
    # 同行正则误杀；但纯同行自广告（无 needed/wanted）仍须被拦截。
    assert lfe.is_competitor(
        title="Die casting supplier needed for our project") is False
    assert lfe.is_competitor(
        title="CNC machining manufacturer wanted") is False
    # 纯同行自广告（无 needed/wanted）仍被识别
    assert lfe.is_competitor(title="Die Casting Supplier") is True


def test_is_competitor_spares_real_buyer_with_supplier_vocab():
    # A2.2-5 买方意图优先：即便标题/正文含「工艺 + 供应商角色」组合词，
    # 只要明确表达真实买方意图，就不误判为同行。
    assert lfe.is_competitor(
        title="Aluminum Die Casting Services",
        snippet="We are looking for a die casting supplier for our project") is False
    assert lfe.is_competitor(
        title="CNC Machining Manufacturer",
        snippet="Our company needs to outsource 5000 custom brackets") is False


def test_recover_watch_leads_rescues_b_class():
    # 严格过滤落选但含「弱意向 + 制造词」组合的候选，应被回收为 B 类待观察线索；
    # 明确同行、或已通过买方闸门的，不应进入 watch 池。
    candidates = [
        # 未过买方闸门（无显式买方动词、URL 也无 RFQ 信号）+ 含 watch 信号 + 非同行
        # -> 回收为 B 类待观察线索（_watch=True）。注意 URL 刻意不含 rfq / 图纸扩展名，
        # 否则会被 RFQ_PLATFORM_RE 判为 A 类求购贴、不再进入 watch 池。
        {"need_summary": "looking for custom aluminum casting parts",
         "keyword": "sourcing die casting", "source_url": "https://forum-x.com/thread"},
        # 明确同行自广告 -> 不回收
        {"need_summary": "we are a manufacturer of die casting",
         "keyword": "x", "source_url": "https://acme-cast.com"},
        # 已通过买方闸门（显式买方动词）-> 不进 watch 池（属于 A 类，已在主流程）
        {"need_summary": "we are looking for a die casting supplier",
         "keyword": "x", "source_url": "https://buyer-co.com/rfq"},
    ]
    watch = lfe.recover_watch_leads(candidates)
    assert len(watch) == 1
    assert watch[0]["_watch"] is True
    assert "forum-x.com" in watch[0]["source_url"]


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
