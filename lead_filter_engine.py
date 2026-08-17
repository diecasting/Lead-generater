#!/usr/bin/env python3
"""
lead_filter_engine.py — 通用 B2B 线索「反同行 + 买方闸门」过滤引擎
================================================================

把「从搜索结果里甄别真实买家 / 外包商、剔除同行供应商自广告」的逻辑，
抽离成一套**与具体项目无关**的可复用工具。可直接 `import` 用于任何类似
B2B 线索搜集项目（压铸 / CNC / 注塑 / 五金 / 机加工……），无需依赖 main.py。

提供三大核心能力：

1. 同行邮箱智能识别与拦截   ->  is_competitor_email()
2. 硬核同行文本特征黑名单   ->  is_competitor() / filter_competitors()
3. 严格买方意图过滤 + 负向预查闸门 ->  is_true_buyer() / passes_buyer_gate()

所有规则均为「确定性规则」，不依赖任何外部 API，便于测试、审计与跨项目复用。

------------------------------------------------------------------
使用示例（快速上手）
------------------------------------------------------------------
>>> from lead_filter_engine import (
...     is_competitor_email, is_competitor, is_true_buyer,
...     filter_competitors, filter_competitor_emails, filter_leads,
... )
>>>
>>> # 1) 一眼识别同行联络邮箱
>>> is_competitor_email("yongzhucasting@163.com")     # True  —— 前缀含 casting
>>> is_competitor_email("sales@abc-machining.com")    # True  —— 域名含 machining
>>> is_competitor_email("acmetech@gmail.com")         # False —— 弱信号词 + 免费邮箱，放行
>>>
>>> # 2) 判断一条搜索结果是不是同行自广告
>>> is_competitor(title="We are a manufacturer of die casting",
...               snippet="Our foundry offers ISO certified casting")   # True
>>>
>>> # 3) 判断正文是不是真实买方意图（自动排除供应商反向邀约）
>>> is_true_buyer("We are looking for a die casting supplier")          # True
>>> is_true_buyer("Request a quote from us for your project")          # False —— 反向邀约
>>>
>>> # 4) 一条龙：文本反同行 + 买方闸门，返回存活的原始线索
>>> survivors = filter_leads(raw_search_results)
"""

import re
import sys


# ===========================================================================
# 一、同行邮箱识别规则（Anti-Competitor by Email）
# ===========================================================================
# 同行供应商的联络邮箱，其前缀（local）或域名（domain）往往直接暴露制造 / 加工身份，
# 例如 yongzhucasting@…、sales@abc-machining.com、info@xyz-foundry.com 等。
# 这类邮箱一眼即可判定为同行，必须直接拦截，不能当作买家跟进邮箱。
#
# 识别策略（两级）：
#   强信号  —— local 或 domain 含明确制造/加工词 -> 直接判定为同行；
#   弱信号  —— 仅 domain 含较泛的制造词，且不是免费/个人邮箱域名时才判为同行
#             （避免误伤 acmetech@gmail.com 这类普通买家邮箱）。

COMPETITOR_EMAIL_STRONG = (
    "casting", "foundry", "machining", "molding", "moulding", "tooling",
    "diecast", "cnc", "fabricat", "stamping", "forging", "mold", "mould",
    "mill", "lathe", "weld", "anodiz", "galvaniz", "extrud", "metalwork",
)
COMPETITOR_EMAIL_WEAK = (
    "parts", "manufacturer", "factory", "supplier", "workshop",
    "industry", "industrial", "machinery", "tech",
)

# 免费 / 个人邮箱域名白名单：弱信号命中时，只要域名在此名单内一律放行，
# 最大限度降低误伤真实买家的概率。
FREE_EMAIL_DOMAINS = (
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "163.com", "qq.com", "126.com", "protonmail.com", "icloud.com",
)


def is_competitor_email(email):
    """判断一个邮箱是否属于同行供应商（而非真实买家）。命中即应丢弃。

    Args:
        email (str): 待判断的邮箱地址。

    Returns:
        bool: True 表示疑似同行供应商邮箱（应拦截）；False 表示放行。

    示例：
        >>> is_competitor_email("yongzhucasting@163.com")
        True
        >>> is_competitor_email("acmetech@gmail.com")
        False
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return False
    local, _, domain = e.rpartition("@")
    if not local or not domain:
        return False
    # 强信号：前缀或域名含明确制造/加工词
    if any(tok in local for tok in COMPETITOR_EMAIL_STRONG):
        return True
    if any(tok in domain for tok in COMPETITOR_EMAIL_STRONG):
        return True
    # 弱信号：仅当域名命中且不是免费/个人邮箱域名（降低误伤买家的概率）
    if domain not in FREE_EMAIL_DOMAINS:
        if any(tok in domain for tok in COMPETITOR_EMAIL_WEAK):
            return True
    return False


# ===========================================================================
# 二、同行文本特征黑名单（Anti-Competitor Text / Negative Filtering）
# ===========================================================================
# 以下短语天然是「供应商在推销自己」，与我们要找的买家完全相反；命中即判定为
# 同行 / 无效页面，在清洗阶段直接剔除。只保留正在寻源、询盘、外包或发布采购
# 需求的买家（Buyer）、品牌商（Brand owner）、采购经理（Purchasing manager）
# 或产品开发公司（Product development company）。

COMPETITOR_HARD_PHRASES = (
    "we are a manufacturer", "we are an oem", "we are a supplier",
    "we are a foundry", "we're a manufacturer", "we are a precision",
    "our foundry", "our factory", "our own factory",
    "our manufacturing facility", "our production facility",
    "machining services provider",
    "injection molding supplier", "iso certified factory",
    "iso certified manufacturer",
    "we provide manufacturing services", "we specialize in manufacturing",
    "precision manufacturer since", "leading manufacturer of",
    "leading supplier of", "leading foundry", "one-stop manufacturing",
    "turnkey manufacturing", "we offer die casting",
    "we offer cnc machining", "we offer injection molding",
    "welcome to our factory",
    # 供应商在「邀请别人向自己询价」（区别于买家主动询盘）
    "request a quote from us", "request quote from us",
    "get a quote from us", "contact us for a quote", "ask us for a quote",
    "request for quote from us",
    # 供应商自述特征（"我们生产 / 我们提供服务" 逻辑）—— 仅保留显式第一人称制造动词，
    # 已移除 casting capabilities / custom manufacturing solutions / iso certified /
    # our capabilities / our production line 等泛化制造词汇（见 A2.2-5，避免误杀中立页面）
    "we manufacture", "we fabricate", "we produce",
)

# 高精度的「自广告主语 + 供应商名词」正则：必须出现 we/our + 供应商名词，
# 且中间不夹带 looking for / seeking / need 等买方动词，避免误杀真实买家。
COMPETITOR_REGEX = re.compile(
    r"(?:"
    r"we (?:are|'re) (?:a|an|the) (?:leading |precision |professional |global |"
    r"reliable |trusted |top )*(?:manufacturer|supplier|factory|foundry|molder|"
    r"machine shop|producer|fabricator)\b"
    r"|our (?:own )?(?:foundry|factory|facility|plant|workshop|tooling|machine shop)\b"
    r"|(?:cnc |die ?casting |injection molding |metal )?(?:machining|casting|"
    r"molding|stamping) services (?:provider|company|supplier)\b"
    # 供应商自述动词 + 制造名词（"我们生产/专做/提供…制造服务" 逻辑）
    r"|we (?:specialize|manufacture|produce|fabricate) (?:in |our |the |a |an |and )?"
    r"(?:manufactur|machin|cast|mold|mould|cnc|metal|precision|injection|"
    r"production|fabricat|part|component|tooling|stamping)\b"
    r"|we provide (?:our |the |a |an )?(?:manufactur|machin|cast|mold|cnc|"
    r"production|fabricat) .{0,40}services\b"
    # 供应商在邀请「别人向自己询价」（区别于买家主动 request for quote）
    r"|request (?:a |us )?quote from us|get a quote from us|contact us for a quote"
    r")",
    re.I,
)

# B1 新增：通用「供应商身份」标题/短语识别（组合语义，非单词匹配）
# 形式：工艺词（die casting / casting / machining / cnc / molding ...）+ 供应商角色名词
# （supplier / manufacturer / services / provider / foundry ...）。刻意杜绝单一词误判
# （buyer 页面里也常出现 manufacturer / factory / production），只认「工艺 + 角色」组合。
COMPETITOR_TITLE_RE = re.compile(
    r"\b(?:alumin(?:ium|um)|zinc|magnesium|metal|die[- ]?cast|diecast|"
    r"casting|injection[- ]?mold(?:ing)?|cnc|precision|machin|mold(?:ing)?|"
    r"stamping|fabricat|forg|tool)"
    r"(?:[- ])?"
    r"(?:casting|machining|molding|moulding|stamping)?"  # 可选中间工艺词
    r"\s+(?:supplier|manufacturer|services|producer|provider|maker|"
    r"company|foundry|workshop|factory|facility)\b",
    re.I,
)


def is_competitor(url="", title="", snippet=""):
    """判定一条结果是否属于同行供应商自广告（而非真实买家）。

    命中即视为无效 / 同行页面，应在清洗阶段剔除，只保留正在寻源、询盘、
    外包或发布采购需求的买家（Buyer）、品牌商、采购经理或产品开发公司。
    同时扫描标题、摘要与 URL，覆盖「标题 / URL / 正文」三种特征来源。

    Args:
        url (str):     结果 URL（常含域名 / 路径特征）。
        title (str):   结果标题。
        snippet (str): 结果摘要 / 正文片段。

    Returns:
        bool: True 表示疑似同行自广告，应剔除。

    示例：
        >>> is_competitor(title="We are a manufacturer of die casting",
        ...               snippet="Our foundry offers ISO certified casting")
        True
    """
    text = f"{title} {snippet} {url}".lower()
    # 0) 买方意图优先（A2.2-5）：若文本明确表达真实买方意图（TRUE_BUYER_RE 命中），
    #    一律视为求购 / 外包页面，不误判为同行——即便其夹带部分制造词汇。这是
    #    「买方意图明显的页面优先保留」的核心保障，可杜绝把真实买家当同行误杀。
    if TRUE_BUYER_RE.search(text):
        return False
    # 1) 既有硬短语 / 自广告正则（仅保留「明确自称 Manufacturer/Factory/Foundry
    #    或主动反向邀约」的强信号；泛化制造词汇已移除以避免误杀中立页面）
    if any(p in text for p in COMPETITOR_HARD_PHRASES):
        return True
    if COMPETITOR_REGEX.search(text):
        return True
    # 2) B1 通用「工艺 + 供应商角色」组合短语（如 Die Casting Supplier、
    #    Aluminum Die Casting Services、CNC Machining Manufacturer）。命中即视为
    #    同行 / 供应商自广告——除非同一文本明显表达「真实买方意图」，放行交由买方闸门。
    if COMPETITOR_TITLE_RE.search(text) and not TRUE_BUYER_RE.search(text):
        return True
    return False


def filter_competitors(raw_results):
    """过滤掉同行供应商自广告页面，仅保留潜在真实买家线索。

    Args:
        raw_results (list[dict]): 搜索原始结果，每条含 'url'/'title'/'snippet' 之一。

    Returns:
        list[dict]: 剔除同行自广告后的结果列表。
    """
    kept, dropped = [], 0
    for r in raw_results:
        if is_competitor(r.get("url", ""), r.get("title", ""), r.get("snippet", "")):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        print(f"[lead_filter] 已过滤 {dropped} 条同行/供应商自广告结果。",
              file=sys.stderr)
    return kept


# ===========================================================================
# 三、严格买方意图过滤 + 负向预查闸门（True Buyer Intent Gate）
# ===========================================================================
# 真正的终端买家 / 外包商，其文本通常带有独特的求购行为特征。以下正则只捕获
# 「买家亲口说出」的采购 / 寻源 / 外包动作；其中关键陷阱是用负向预查 `(?! from)`
# 排除 "request a quote from us" 这类**供应商反向邀约**（别人找我们询价，而非我们
# 找别人），确保只放行真实买家。

TRUE_BUYER_RE = re.compile(
    r"we (?:are|'re) looking for|"                                       # we are looking for
    r"looking for (?:a |an |our |the )?(?:supplier|manufacturer|quote|quotation|"
    r"price|pricing|partner|"
    r"vendor|factory|oem|odm|foundry|molder)\b|"                          # looking for a supplier / quotation / pricing
    r"seeking (?:a |an |our |the )?(?:supplier|manufacturer|quote|partner|"
    r"vendor|oem|odm|foundry)\b|"                                         # seeking a supplier
    r"our (?:company|team|project|firm|organization) (?:needs|requires|"
    r"is looking for|is seeking|needs to|wants to)|"                       # our company needs
    r"our project requires|our team needs|"                               # 项目明确需要
    r"we need (?:a |to |quote|supplier|manufacturer|partner)|"            # we need a quote / we need to source
    r"need to (?:source|outsource|procure|order|purchase|buy|find|get)|"  # we need to outsource
    r"request(?:ing)? (?:a |for )?(?:quote|quotation)(?! from)|"        # requesting a quote（排除 "from us" 反向邀约）
    r"request for (?:quote|quotation)|"                                   # request for quote
    r"need (?:a |the )?pricing for|"                                       # need pricing for
    r"sourcing (?:parts|components|suppliers|manufacturers|partners)\b|"   # sourcing parts
    r"manufacturing partner needed|"                                       # manufacturing partner needed
    r"supplier(?:s)? required|"                                            # supplier required
    r"help (?:us|me) (?:find|source|get|obtain) (?:a |an )?"
    r"(?:supplier|quote|manufacturer|partner)|"                           # help us find a supplier
    r"looking to (?:source|outsource|procure|partner|buy|order|find)|"    # looking to source
    r"we want to (?:source|outsource|procure|order|buy|find|partner)|"    # we want to source
    r"wish to (?:purchase|buy|source|procure)|"                           # wish to purchase
    r"interested in (?:purchasing|buying|sourcing|procuring)|"            # interested in purchasing
    r"in need of|sourcing (?:for|a |the )|"                               # in need of / sourcing for
    r"procurement|tender|bid (?:for|request)|"                            # procurement / tender
    r"buy (?:from|the|these)|purchase (?:from|order)|"                    # buy from
    r"quote request|quotation request"                                    # quote / rfq
    # —— A2.2-5 包容性增强：海外采购商常见询价 / 寻源表达（动词化、精准，避免误伤同行）——
    r"please (?:send|provide|share|quote|give) (?:us )?(?:a |your )?"
    r"(?:quote|quotation|price|pricing|proposal)|"                         # please send us a quote
    r"send (?:us )?(?:a |your )?(?:quote|quotation|price|pricing)|"        # send your quotation
    r"quote (?:needed|required|requested|please)|"                         # quote needed
    r"price (?:inquiry|enquiry|request|list)|"                             # price inquiry
    r"best price for|"                                                     # best price for
    r"we are in the market for|"                                           # we are in the market for
    r"we require (?:a |an |the )?(?:die[- ]?cast(?:ing)? |alumin\w* |zinc |"
    r"metal |cnc |precision |custom |our )?(?:supplier|manufacturer|quote|"
    r"partner|foundry|oem|odm)|"                                           # we require a supplier
    r"(?:supplier|manufacturer|foundry|factory|molder|oem|odm|partner|"
    r"vendor)s? (?:needed|required|wanted)|"                                # supplier needed（买家求供，非同行自广告）
    r"looking to buy|want to buy|wish to buy|interested in buying|"        # 明确购买意图
    r"need to (?:place|make) (?:an |a )?order|ready to order|"             # 准备下单
    r"\bimport (?:from )?(?:china|overseas)|"                              # 海外采购信号
    r"sourcing (?:from|in) china|china (?:sourcing|procurement)|"
    r"overseas supplier|global supplier|worldwide supplier|"
    r"find (?:a |an )?(?:china|overseas) (?:supplier|manufacturer)",
    re.I,
)
# 采购平台 / 黄页上的真实求购贴信号：RFQ 标识、图纸 / 规格需求（非制造企业发出的
# 组装 / 设计需求）。用于放行那些没有显式买方动词、但确实是求购贴的结果。
RFQ_PLATFORM_RE = re.compile(
    r"\brfq\b|request for quote|request for quotation|quotation request|"
    r"quote request|rfq[#\s\-]?\d|enquiry|inquiry|price request|bid request|"
    r"(?:drawing|cad|step|iges|dxf|\bstp\b|blueprint|3d model|technical spec)\b",
    re.I,
)


def is_true_buyer(text):
    """判断一段文本是否表达「真实买方意图」（买家在主动寻源 / 询盘 / 外包）。

    命中条件（满足任一即可）：
    1. 显式买方动作（TRUE_BUYER_RE）：we are looking for a supplier、
       seeking a manufacturer、our company needs、need to outsource、
       requesting a quote（注意：带负向预查 `(?! from)`，已排除
       "request a quote from us" 这类供应商反向邀约）……
    2. 采购平台 / 黄页上的真实求购贴（RFQ_PLATFORM_RE）：RFQ 编号、
       图纸 / CAD / STEP 需求等非制造企业发出的组装 / 设计需求。

    Args:
        text (str): 标题 + 摘要 + 关键词拼接后的文本（建议先 .lower()）。

    Returns:
        bool: True 表示属于真实买方意图。

    示例：
        >>> is_true_buyer("We are looking for a die casting supplier")
        True
        >>> is_true_buyer("Request a quote from us for your project")
        False
    """
    return bool(TRUE_BUYER_RE.search(text) or RFQ_PLATFORM_RE.search(text))


def passes_buyer_gate(text):
    """买方闸门：语义化入口，等价于 is_true_buyer()。

    内置的负向预查 `(?! from)` 是闸门的关键——它把 "request a quote from us"
    （供应商在邀请别人向自己询价，即反向邀约）排除在买方动作之外，
    确保只放行真正由买家发出的求购动作。
    """
    return is_true_buyer(text)


# ===========================================================================
# 三之二、B 类 / 待观察线索兜底（防每日 0 条）
# ===========================================================================
# 当严格过滤（同行闸门 + 买方闸门）后确实为 0 条时，从被买方闸门剔除的候选里
# 回收「中 / 低意向但带有强 RFQ / 寻源信号」的页面，归为 B 类（待观察）线索，
# 避免日报完全空白。这类页面不具备显式买方动词，但同时含「弱意向词 + 制造 / 零件
# 词」组合，仍可能是真实外包 / 采购需求，值得人工二次确认。
WATCH_INTENT_RE = re.compile(
    r"\b(looking for|need|needs|needed|require|requires|required|"
    r"sourcing|partner needed|quotation|rfq|request quote|request a quote|"
    r"prototype|prototyping|custom|oem|odm|outsource|outsourcing|project|"
    r"want|want to|seek|seeking|buy|buying|purchase|purchasing|order|ordering|enquiry|inquiry|import|overseas)\b",
    re.I,
)
WATCH_DOMAIN_RE = re.compile(
    r"\b(alumin|zinc|magnes|metal|die ?cast|casting|injection ?mold|"
    r"cnc|machin|mold|stamp|fabricat|forg|tool|part|component|"
    r"supplier|manufactur|precision|hardware|enclosure|housing|bracket)\b",
    re.I,
)


def has_watch_signal(text):
    """判断文本是否含「弱意向词 + 制造 / 零件词」组合（B 类待观察线索信号）。"""
    return bool(WATCH_INTENT_RE.search(text) and WATCH_DOMAIN_RE.search(text))


def recover_watch_leads(candidates):
    """兜底：从严格过滤落选的候选中回收 B 类（待观察）线索。

    仅回收满足以下全部条件的候选：
    1. 不是明确同行自广告（通过 is_competitor 复检）；
    2. 未通过严格买方闸门（passes_buyer_gate 为 False）；
    3. 含弱意向 + 制造 / 零件组合信号（has_watch_signal 为 True）。

    返回的每条线索带 ``_watch=True`` 标记，便于下游降权 / 标注。

    Args:
        candidates (list[dict]): clean_with_ai 之后的候选线索（含 need_summary /
            keyword / source_url 等字段）。

    Returns:
        list[dict]: 回收的 B 类待观察线索（已打 _watch 标记）。
    """
    watch = []
    for l in candidates:
        url = l.get("source_url", "")
        summary = l.get("need_summary", "")
        if is_competitor(url, summary, ""):
            continue
        blob = " ".join(filter(None, [summary, l.get("keyword", ""), url])).lower()
        if not passes_buyer_gate(blob) and has_watch_signal(blob):
            item = dict(l)
            item["_watch"] = True
            watch.append(item)
    return watch


# ===========================================================================
# 四之二、7 类公司角色分类（与 is_competitor / is_true_buyer 解耦的辅助标签）
# ===========================================================================
# 在确定性闸门之外，额外给每条线索打一个「公司角色」标签，便于下游（报表 / CRM）
# 做分群，而不会影响现有的 competitor / buyer 布尔判定。判定优先级自上而下：
#   COMPETITOR > DISTRIBUTOR > SERVICE_PROVIDER > SUPPLIER > OEM > BUYER > IRRELEVANT
# 关键约束：不删除、不替换 is_competitor / is_true_buyer，仅新增一个独立维度；
# 也不要把 manufacturer 简单等同于 COMPETITOR（manufacturer 未触及同行硬规则时归
# SUPPLIER），也不要把 OEM 简单等同于 BUYER（OEM 仅在同时表达采购意图时才归 OEM）。

COMPANY_CLASSES = (
    "BUYER", "SUPPLIER", "COMPETITOR", "OEM", "DISTRIBUTOR",
    "SERVICE_PROVIDER", "IRRELEVANT",
)

_DISTRIBUTOR_RE = re.compile(
    r"\b(?:distribut|reseller|wholesal|stockist|dealer|trader)", re.I)
_SERVICE_PROVIDER_RE = re.compile(
    r"service provider|engineering services|\bconsult|"
    r"prototyp(?:ing)? service|3d print(?:ing)? service|design service|"
    r"sourcing (?:agent|service)|trading company|import(?:ing|er)? company|"
    r"\bagent\b|\bbroker\b", re.I)
_SUPPLIER_RE = re.compile(
    r"we (?:manufacture|produce|supply|fabricate|make|provide)|"
    r"our (?:products|parts|components)|"
    r"(?:oem|odm) (?:manufacturer|supplier|factory)|"
    r"supplier of|manufacturer of|factory of|exporter of", re.I)


def classify_company(lead):
    """给一条线索打上 7 类公司角色标签（与 is_competitor / is_true_buyer 解耦）。

    仅做「辅助分类」，不改变任何过滤 / 评分逻辑；真正决定入库与否的仍是
    is_competitor 与 passes_buyer_gate。返回值为 COMPANY_CLASSES 之一。

    Args:
        lead (dict): 含 'need_summary' / 'keyword' / 'source_url' / 'company' 之一。

    Returns:
        str: 公司角色标签。

    示例：
        >>> classify_company({"need_summary": "We are looking for a die casting supplier",
        ...                   "keyword": "looking for die casting supplier"})
        'BUYER'
    """
    blob = " ".join(filter(None, [
        lead.get("need_summary", ""),
        lead.get("keyword", ""),
        lead.get("source_url", ""),
        lead.get("company", ""),
    ])).lower()
    return _classify_text(blob)


def _classify_text(text):
    """基于确定证据的 7 类分类（见 classify_company 的优先级说明）。"""
    t = (text or "").lower()
    # 1) 同行 / 供应商自广告（复用既有硬规则）
    if is_competitor("", t, t):
        return "COMPETITOR"
    # 2) 经销商 / 分销商
    if _DISTRIBUTOR_RE.search(t):
        return "DISTRIBUTOR"
    # 3) 服务方（设计 / 工程 /  sourcing agent / 贸易，非制造）
    if _SERVICE_PROVIDER_RE.search(t):
        return "SERVICE_PROVIDER"
    # 4) 供应商（制造 / 供货能力，但未触及同行硬规则）
    if _SUPPLIER_RE.search(t):
        return "SUPPLIER"
    # 5) OEM（既提 OEM/ODM 又表达采购意图）
    if re.search(r"\boem\b|\bodm\b", t) and is_true_buyer(t):
        return "OEM"
    # 6) 真实买方
    if is_true_buyer(t):
        return "BUYER"
    # 7) 其它（新闻 / 教程 / 无关）
    return "IRRELEVANT"


# ===========================================================================
# 四、邮箱提取后处理 + 一键流水线
# ===========================================================================
def filter_competitor_emails(leads):
    """邮箱提取之后调用：剔除线索中属于同行的联络邮箱。

    规则：
    - 若一条线索提取到的邮箱「全部」都是同行邮箱，则进一步判断——
      若正文本身已是明确的买方意图，则只剔除同行邮箱、保留该线索
      （可能变成无线索邮箱，避免误杀真实买家）；
      若正文无法确认是买家，则整页很可能就是同行自广告，直接丢弃。
    - 否则仅剥离其中的同行邮箱，保留其余邮箱与线索。

    Args:
        leads (list[dict]): 已含 'emails' 字段的线索列表。

    Returns:
        list[dict]: 过滤后的线索列表。
    """
    kept, dropped = [], 0
    for l in leads:
        emails = l.get("emails") or []
        non_comp = [e for e in emails if not is_competitor_email(e)]
        if emails and not non_comp:
            text = f"{l.get('need_summary','')} {l.get('keyword','')} " \
                   f"{l.get('source_url','')}".lower()
            if TRUE_BUYER_RE.search(text):
                # 正文明确是买家，仅剔除同行邮箱，保留线索（可能无线索邮箱）
                l["emails"] = []
                kept.append(l)
            else:
                # 正文无法确认是买家 -> 整页很可能就是同行自广告，丢弃
                dropped += 1
                continue
        else:
            l["emails"] = non_comp
            kept.append(l)
    if dropped:
        print(f"[lead_filter] 已过滤 {dropped} 条「邮箱全为同行供应商且非买方正文」的线索。",
              file=sys.stderr)
    return kept


def filter_leads(raw_results, *, require_buyer_intent=True):
    """一条龙流水线：文本反同行 +（可选）买方闸门，返回存活的原始线索。

    顺序：
    1. filter_competitors  —— 剔除同行自广告文本 / URL 页面；
    2. （若 require_buyer_intent）对每条结果用 is_true_buyer 复查，
       仅保留表达真实买方意图者，从根本上杜绝供应商自广告入库。

    Args:
        raw_results (list[dict]): 搜索原始结果（含 title/snippet/keyword/url）。
        require_buyer_intent (bool): 是否启用买方闸门（默认 True）。

    Returns:
        list[dict]: 通过两道过滤后的原始线索列表。
    """
    survivors = filter_competitors(raw_results)
    if not require_buyer_intent:
        return survivors
    out = []
    for r in survivors:
        blob = f"{r.get('title','')} {r.get('snippet','')} {r.get('keyword','')}".lower()
        if is_true_buyer(blob):
            out.append(r)
    return out


# ===========================================================================
# 自测 / 演示
# ===========================================================================
if __name__ == "__main__":
    checks = [
        # (描述, 实际值, 期望值)
        ("同行邮箱(前缀 casting)", is_competitor_email("yongzhucasting@163.com"), True),
        ("同行邮箱(域名 machining)", is_competitor_email("sales@abc-machining.com"), True),
        ("弱信号+免费邮箱(放行)", is_competitor_email("acmetech@gmail.com"), False),
        ("正常买家邮箱(放行)", is_competitor_email("buyer@brandco.com"), False),
        ("同行文本(we are a manufacturer)", is_competitor(title="We are a manufacturer of die casting"), True),
        ("同行文本(反向邀约短语)", is_competitor(snippet="request a quote from us for your project"), True),
        ("真实买方意图(looking for)", is_true_buyer("We are looking for a die casting supplier"), True),
        ("供应商反向邀约(应排除)", is_true_buyer("Request a quote from us for your project"), False),
        ("求购贴(RFQ 编号)", is_true_buyer("RFQ #2026-883 need custom aluminum bracket"), True),
    ]
    ok = 0
    for desc, got, exp in checks:
        status = "PASS" if got == exp else "FAIL"
        if got == exp:
            ok += 1
        print(f"[{status}] {desc}: got={got} expected={exp}")
    print(f"\n{ok}/{len(checks)} 通过")
    raise SystemExit(0 if ok == len(checks) else 1)
