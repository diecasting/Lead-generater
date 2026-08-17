#!/usr/bin/env python3
"""
Daily Lead Collector — CNC / Die Casting / Casting / Plastic Injection Molding

每天自动扫描网络，针对以下垂直行业寻找近期有采购意图的潜在客户（Leads）：
  - CNC 加工 (CNC machining)
  - 压铸 (Die casting)
  - 铸造 (Casting)
  - 塑胶模具与注塑 (Plastic injection molding & parts)

流程：
  1. 多关键词搜索（Bing Web Search API，未配置 Key 时回退到 DuckDuckGo 公开搜索）
     —— 关键词采用「买方意图」分类矩阵（压铸/铸造买家、塑胶注塑买家、
        CNC 精密加工买家、外包/OEM-ODM 采购买家），全部从采购方视角撰写
        （looking for / seeking / need / outsourcing / RFQ），而非供应商自广告；
        支持 SEARCH_COMBINE 组合长尾查询，单次覆盖更多维度与原始线索
  2. 优先调用大模型 API 清洗、过滤垃圾信息；遇 429 / 超时 / 无额度时自动
     指数退避重试，并回退到本地规则清洗，确保不丢线索
  3. 网页邮箱提取：对每条线索的来源页面用正则抓取并过滤真实联系邮箱
     （过滤静态资源后缀、example.com 占位、no-reply 等垃圾邮箱）
  4. 垃圾站点过滤：剔除知乎 / 维基 / 博客 / 中介广告等，只留真实买家；
     并引入垂直黄页与专业社区定向搜索（site:thomasnet.com 等）
  5. 意向评分(0-100)：依据置信度、真实企业邮箱、具体工艺/材质/图纸/采购数量加权，
     按分数排序并打上 🔥 高意向 / ⚡ 中意向 标签；同时为每条线索生成个性化英文开发信
  6. 生成美观、响应式的 HTML 日报（含意向分、可点击来源、✉️ 邮箱、开发信草稿），
     通过 SMTP (SSL / STARTTLS) 发送；并按 sent_cache.json 历史去重，避免重复推送

所有敏感配置均来自环境变量 / GitHub Secrets，不写死在代码中。
"""

import os
import re
import json
import sys
import time
import ssl
import smtplib
import socket
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 反同行 + 买方闸门过滤引擎（独立、可复用模块；见 lead_filter_engine.py）
from lead_filter_engine import (
    FREE_EMAIL_DOMAINS,
    TRUE_BUYER_RE,
    RFQ_PLATFORM_RE,
    is_competitor,
    is_competitor_email,
    passes_buyer_gate,
    classify_company,
    filter_competitors,
    filter_competitor_emails,
    recover_watch_leads,
)


# ---------------------------------------------------------------------------
# 配置（全部来自环境变量 / GitHub Secrets）
# ---------------------------------------------------------------------------

# 目标行业关键词矩阵 —— 全部从「买家 / 采购方」视角撰写（关键！）
# 旧的矩阵混入了大量「供应商自广告」视角的关键词（如 "mold maker RFQ China"、
# "CNC machining contract manufacturer"、"buyer seeking factory CNC"），导致搜索
# 结果回灌进来的全是同行压铸/加工工厂。这里一律改写为「正在寻找 / 外包 / 询盘」
# 的买方措辞（looking for / seeking / need / want to source / outsourcing / RFQ /
# sourcing inquiry / buyer），即使句中带有 supplier / manufacturer，也只作为
# 「买家要找的对象」，而非供应商自己在吆喝。
KEYWORD_GROUPS = {
    # 压铸 / 铸造 买家（寻找压铸/铸造外包的采购方）
    "压铸与铸造买家": [
        "looking for die casting supplier",
        "need custom aluminum die casting parts",
        "aluminum die casting RFQ buyer",
        "request for quote die casting",
        "outsourcing die casting production",
        "seeking zinc die casting manufacturer",
        "custom product development die casting buyer",
        "want to source die cast components",
        "sand casting buyer inquiry",
        "aluminum die casting sourcing inquiry",
    ],
    # 塑胶模具 / 注塑 买家
    "塑胶模具与注塑买家": [
        "OEM plastic mold RFQ",
        "looking for injection molding supplier",
        "need custom plastic parts molded",
        "seeking plastic injection mold maker",
        "plastic injection molding buyer inquiry",
        "outsource plastic molding production",
        "custom plastic product development buyer",
        "request quotes injection molded parts",
        "molder wanted plastic components",
        "injection molding sourcing agent",
    ],
    # CNC / 精密加工 买家
    "CNC与精密加工买家": [
        "contract manufacturing partner machining",
        "seeking CNC machining supplier",
        "need custom CNC machined parts",
        "CNC machining RFQ buyer",
        "looking for precision machining partner",
        "outsource CNC machining production",
        "custom CNC parts sourcing inquiry",
        "5 axis machining buyer request",
        "rapid prototyping machining RFQ",
        "OEM CNC milling buyer",
    ],
    # 外包 / OEM-ODM / 产品开发 采购买家
    "外包与OEM_ODM采购买家": [
        "outsourcing metal casting production",
        "contract manufacturing partner wanted",
        "OEM ODM inquiry custom parts",
        "looking for manufacturing partner China",
        "metal parts sourcing agent buyer",
        "product development company seeking supplier",
        "buyer seeking custom manufacturer",
        "import metal components inquiry buyer",
        "distributor looking for custom parts",
        "engineering company sourcing machined parts",
    ],
}

# 组合搜索：将「工艺/材质词」与「买方意图词」交叉，生成更多长尾查询
# （默认关闭，通过 SEARCH_COMBINE=1 开启；组合数量受 SEARCH_COMBINE_MAX 限制）
# 注意：INTENT 侧一律使用买方动词，绝不使用 "supplier"/"factory" 作为主语去吆喝。
COMBINE_PROCESS = [
    "aluminum die casting", "zinc die casting", "CNC machining",
    "plastic injection molding", "precision machining", "metal casting",
]
COMBINE_INTENT = [
    "looking for supplier", "RFQ", "buyer inquiry", "request for quote",
    "sourcing request", "seeking manufacturer", "contract manufacturing partner",
    "need custom parts", "outsourcing production", "OEM ODM inquiry",
]


def get_search_keywords():
    """返回本次运行要检索的关键词列表（分类展开 + 可选组合），去重保序。"""
    kws = []
    for group in KEYWORD_GROUPS.values():
        kws.extend(group)
    if str(os.getenv("SEARCH_COMBINE", "")).lower() in ("1", "true", "yes"):
        combos = [f"{p} {i}" for p in COMBINE_PROCESS for i in COMBINE_INTENT]
        maxc = int(os.getenv("SEARCH_COMBINE_MAX", "8"))
        kws.extend(combos[:maxc])
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


SEARCH_PER_KEYWORD = int(os.getenv("SEARCH_PER_KEYWORD", "10"))
RESULTS_LIMIT = int(os.getenv("LEADS_LIMIT", "20"))

# AI 容灾：指数退避重试（应对 429 / 超时 / 网络抖动）
MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "2"))
RETRY_BASE_DELAY = int(os.getenv("AI_RETRY_BASE_DELAY", "3"))  # 秒

# 历史去重：已推送线索缓存文件路径
HISTORY_FILE = os.getenv("HISTORY_FILE", "sent_cache.json")
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "2000"))  # 防止缓存无限增长

# A1 — 去重 TTL（天）：同一线索在 TTL 天内重复出现则去重；超过 TTL 允许重新进入
# discovery pipeline。把"永久去重"改造成"有生命周期的去重"，避免可推 Unique 空间
# 随运行次数永久收敛。默认 30 天，可经环境变量覆盖。
DISCOVERY_DEDUP_TTL_DAYS = int(os.getenv("DISCOVERY_DEDUP_TTL_DAYS", "30"))

# A2.1 — Bing 搜索时间窗（freshness）。仅影响配置了 BING_API_KEY 的 Bing 搜索路径；
# DDG 回退路径不支持 freshness 参数，配置对其无效。默认值 "Week" 与改动前行为完全一致。
# 允许值受 Bing Web Search v7 规范约束；非法值打印警告并回退到 "Week"，discovery 不崩溃。
_ALLOWED_FRESHNESS = ("Day", "Week", "Month")
DISCOVERY_SEARCH_FRESHNESS = os.getenv("DISCOVERY_SEARCH_FRESHNESS", "Week")
if DISCOVERY_SEARCH_FRESHNESS not in _ALLOWED_FRESHNESS:
    print(
        f"[config][WARN] DISCOVERY_SEARCH_FRESHNESS="
        f"{DISCOVERY_SEARCH_FRESHNESS!r} 非法，回退为 'Week'",
        file=sys.stderr,
    )
    DISCOVERY_SEARCH_FRESHNESS = "Week"

# A2.2-1 — 安全实验开关（DRY-RUN）。默认 false，保持生产行为完全不变。
# 设为 "true" / "1" / "yes" / "on" 时：真实执行 search / filter / AI / enrichment，
# 生成 leads_report.json 与 discovery_metrics.json，但完全跳过 send_email()
# 与 save_sent_history()，不连接 SMTP、不写入 sent_cache —— 使 Week vs Month
# 可在不发送邮件、不污染历史缓存的情况下进行真实实验。此开关不影响任何
# Discovery 策略（搜索量 / 阈值 / score / 关键词 / freshness 默认值等均不变）。
_DISCOVERY_DRY_RUN_RAW = os.getenv("DISCOVERY_DRY_RUN", "false").strip().lower()
DISCOVERY_DRY_RUN = _DISCOVERY_DRY_RUN_RAW in ("1", "true", "yes", "on")

# 垂直黄页 / 专业社区定向搜索（site: 限制），可经环境变量覆盖
DIRECTORY_SITES = [
    s.strip()
    for s in os.getenv(
        "DIRECTORY_SITES",
        "thomasnet.com,kompass.com,reddit.com/r/manufacturing,"
        "engineering.com,globalspec.com",
    ).split(",")
    if s.strip()
]
DIRECTORY_SEARCH = str(os.getenv("DIRECTORY_SEARCH", "1")).lower() in ("1", "true", "yes")
DIRECTORY_MAX_QUERIES = int(os.getenv("DIRECTORY_MAX_QUERIES", "12"))


def cfg(name, default=None):
    """读取环境变量，空字符串视为未设置。"""
    val = os.getenv(name)
    return val if val else default


# ---------------------------------------------------------------------------
# 启动期配置校验（Phase 12.1 — 生产级配置校验器）
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """启动配置校验失败。错误信息对人类可读，且绝不包含任何 secret 值。"""


# 邮件配置支持新规范名 (MAIL_HOST / MAIL_USER) 与旧名 (MAIL_SERVER / MAIL_USERNAME) 兼容，
# 这样已部署的工作流（使用 MAIL_SERVER / MAIL_USERNAME）无需改动即可继续工作。
MAIL_HOST_ALIASES = ["MAIL_HOST", "MAIL_SERVER"]
MAIL_USER_ALIASES = ["MAIL_USER", "MAIL_USERNAME"]


def _first_set(aliases, default=None):
    """按顺序返回第一个非空的环境变量；都为空时返回 default。"""
    for name in aliases:
        val = os.getenv(name)
        if val:
            return val
    return default


def validate_config():
    """在任务启动前校验关键环境变量。

    - 缺失 / 无效的必填项 -> 抛出 ConfigError（信息可读，且不泄露任何 secret 值）
    - OPENAI_API_KEY 可选；缺失时仅打印警告，不阻断运行
    """
    errors = []
    warnings = []

    if not _first_set(MAIL_HOST_ALIASES):
        errors.append("MAIL_HOST (或兼容别名 MAIL_SERVER) 未设置 — 邮件服务器地址缺失")

    port_raw = os.getenv("MAIL_PORT")
    if not port_raw:
        errors.append("MAIL_PORT 未设置")
    else:
        try:
            int(port_raw)
        except (TypeError, ValueError):
            # 只报告变量名，不打印变量值，避免泄露配置
            errors.append("MAIL_PORT 必须是合法整数（当前值无法解析为整数）")

    if not _first_set(MAIL_USER_ALIASES):
        errors.append("MAIL_USER (或兼容别名 MAIL_USERNAME) 未设置 — 邮件登录账户缺失")

    if not os.getenv("MAIL_PASSWORD"):
        errors.append("MAIL_PASSWORD 未设置 — 邮件授权码/密码缺失")

    if not os.getenv("OPENAI_API_KEY"):
        warnings.append(
            "OPENAI_API_KEY 未设置：AI 清洗将被跳过，回退为原始结果直通（任务仍会运行）"
        )

    if errors:
        lines = [
            "[config] 启动配置校验失败，已中止运行。请检查 GitHub Secrets / 环境变量：",
            *[f"  ✗ {e}" for e in errors],
        ]
        if warnings:
            lines += [f"  ! {w}" for w in warnings]
        raise ConfigError("\n".join(lines))

    for w in warnings:
        print(f"[config][WARN] {w}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 搜索层
# ---------------------------------------------------------------------------

def bing_search(query, api_key, count=SEARCH_PER_KEYWORD):
    """Bing Web Search（Azure Cognitive Services / Bing Search API）。"""
    endpoint = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {
        "q": query,
        "count": count,
        "mkt": "en-US",
        "freshness": DISCOVERY_SEARCH_FRESHNESS,  # Bing 时间窗，默认 Week，可经 env 覆盖
        "textDecorations": False,
    }
    resp = requests.get(endpoint, headers=headers, params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in data.get("webPages", {}).get("value", []):
        out.append({
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("snippet") or "").strip(),
            "keyword": query,
        })
    return out


def _ddg_decode(href):
    """DuckDuckGo 的链接是经过跳转编码的，提取真实 URL。"""
    if not href:
        return ""
    if "uddg=" in href:
        return parse_qs(urlparse(href).query).get("uddg", [""])[0]
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://duckduckgo.com" + href
    return href


def ddg_search(query, count=SEARCH_PER_KEYWORD):
    """无 Bing Key 时使用的公开搜索回退方案（DuckDuckGo HTML）。"""
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }
    try:
        resp = requests.post(url, data={"q": query}, headers=headers, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ddg] search failed for '{query}': {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("a.result__a")[:count]:
        title = a.get_text(strip=True)
        real_url = _ddg_decode(a.get("href", ""))
        parent = a.find_parent("div", class_="result")
        snippet = ""
        if parent:
            sn = parent.select_one(".result__snippet")
            if sn:
                snippet = sn.get_text(strip=True)
        if real_url:
            out.append({
                "title": title,
                "url": real_url,
                "snippet": snippet,
                "keyword": query,
            })
    return out


def get_directory_queries(keywords):
    """将关键词与垂直站点轮询配对，生成 site: 限定查询（受 DIRECTORY_MAX_QUERIES 限制）。

    例如 `aluminum die casting RFQ site:thomasnet.com`，精准捕获黄页 / 社区的高价值线索。
    """
    if not (DIRECTORY_SEARCH and DIRECTORY_SITES):
        return []
    # 轮询配对：每个站点均匀分摊查询，避免单一站点占满配额
    pairs = []
    for i, kw in enumerate(keywords):
        site = DIRECTORY_SITES[i % len(DIRECTORY_SITES)]
        pairs.append((kw, site))
    return [f"{kw} site:{site}" for kw, site in pairs[:DIRECTORY_MAX_QUERIES]]


def collect_raw_leads():
    bing_key = cfg("BING_API_KEY")
    keywords = get_search_keywords()
    print(f"[search] 共 {len(keywords)} 个检索关键词。")
    results = []

    # 1) 普通关键词搜索（覆盖全网）
    for kw in keywords:
        print(f"[search] '{kw}'")
        try:
            if bing_key:
                results.extend(bing_search(kw, bing_key))
            else:
                results.extend(ddg_search(kw))
        except Exception as e:
            print(f"[search] error on '{kw}': {e}", file=sys.stderr)

    # 2) 定向黄页 / 专业社区搜索（site: 限制）
    dq = get_directory_queries(keywords)
    if dq:
        print(f"[search] 定向黄页/社区查询 {len(dq)} 条（site: 限制）。")
        for q in dq:
            print(f"[search] '{q}'")
            try:
                if bing_key:
                    results.extend(bing_search(q, bing_key))
                else:
                    results.extend(ddg_search(q))
            except Exception as e:
                print(f"[search] error on '{q}': {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# AI 清洗 / 过滤
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a meticulous B2B lead-qualification analyst for a precision "
    "manufacturing company (CNC machining, aluminum die casting, sand/gravity "
    "casting, and plastic injection molding). You receive raw search results "
    "that may include RFQs, buyer inquiries, sourcing posts, marketplace "
    "listings — and a LOT of noise: supplier self-advertising (competitor "
    "factories/molders/foundries promoting their own capabilities), news, "
    "tutorials, and spam. "
    "CRITICAL — keep ONLY real buying intent from the BUYER side: "
    "  - a company / brand owner / purchasing manager / product-development "
    "firm actively LOOKING FOR or SOURCING a supplier, quoting, or outsourcing "
    "production. "
    "STRICTLY DISCARD: "
    "  - any page that is a supplier advertising itself (phrases like "
    "'we are a manufacturer', 'our foundry', 'we specialize in', 'we manufacture', "
    "'casting capabilities', 'machining services provider', 'injection molding "
    "supplier', 'ISO certified factory', 'custom manufacturing solutions', "
    "'our capabilities', etc.) — these are COMPETITORS, not leads; "
    "  - any page whose contact email reveals a competitor (local part or domain "
    "contains casting / foundry / machining / molding / tooling / cnc / parts / "
    "factory / supplier, e.g. 'yongzhucasting@...', 'sales@abc-machining.com'); "
    "  - supplier pages that invite inbound quotes ('request a quote from us', "
    "'get a quote from us') — that means THEY want to be hired, not that they "
    "are buying; "
    "  - generic articles, tutorials, job posts, and spam. "
    "Only KEEP a result if it is clearly a BUYER / brand owner / purchasing "
    "manager / product-development firm actively LOOKING FOR or SOURCING a "
    "supplier, requesting a quote (from a supplier), outsourcing production, or "
    "posting a genuine RFQ (with RFQ number, drawings, or specs). "
    "PRIORITIZE results containing explicit buyer-intent language: "
    "'We are looking for...', 'Our company needs...', 'Requesting quotes for...', "
    "'Looking to source...', 'seeking a supplier', RFQ / request-for-quote. "
    "Return ONLY valid JSON of the form:\n"
    '{"leads": [{"company": str, "need_summary": str, "source_url": str, '
    '"keyword": str, "confidence": "high"|"medium"|"low"}]}\n'
    "company = the buyer/company name if identifiable, else 'Unknown'. "
    "need_summary = a concise (<=40 words) summary of what they need and where "
    "it was posted. source_url = the original result URL. "
    "If nothing qualifies, return {\"leads\": []}."
)


# ---------------------------------------------------------------------------
# 规则清洗（确定性、零依赖；OpenAI 额度不足 / 网络不通时自动回退）
# ---------------------------------------------------------------------------
# 购买意图信号（强）—— 优先匹配「买家亲口说出」的询盘/采购措辞
BUY_SIGNALS = [
    # 强买方意图短语（出现即可判定为真实买家在找供应商）
    (r"we (?:are|'re) looking for", 5),
    (r"our company (?:needs|is looking for|requires|is seeking)", 5),
    (r"looking to (?:source|outsource|procure|find|partner)", 4),
    (r"seeking (?:a |an )?(?:supplier|manufacturer|quote|partner|vendor)", 4),
    (r"requesting (?:a |quotes|quote|quotation)", 4),
    (r"need to source|interested in sourcing|help (?:us|me) find", 4),
    (r"we want to (?:produce|source)|planning to (?:produce|source|outsource)", 3),
    # 通用询价/采购意图
    (r"\brfq\b|request for quote", 4),
    (r"inquir|enquir", 3),
    (r"\bbuyer\b", 3),
    (r"sourc", 2),
    (r"quot", 2),
    (r"purchas|procure", 2),
    (r"looking for|\bneed\b|required|requirement|\bwant\b", 2),
]
# 行业相关性（加分但不代表购买意图）
DOMAIN_SIGNALS = [
    (r"custom|customiz", 1),
    (r"supplier|manufacturer|factory", 1),
    (r"casting|die[- ]?cast|\bcnc\b|machin|mold|mould|injection", 1),
]
# 供应商自广告（减分）—— 仅匹配「供应商在吆喝」的措辞，避免误伤真实买家
# （买家常说 "we are looking for" / "our company needs"，这些不算自广告）
AD_SIGNALS = [
    (r"we (?:provide|offer|supply|manufacture|produce)\b", 1),
    (r"leading (?:supplier|manufacturer|foundry)", 1),
    (r"our company (?:provides|offers|supplies|is a|manufactures)", 1),
    (r"contact us|get a quote", 1),
]
# 纯噪声（新闻 / 教程 / 招聘）
NOISE_SIGNALS = [
    (r"news|article|tutorial|how[- ]to|wiki|definition", 2),
    (r"job|salary|career|hiring|vacancy", 2),
]
# 来自「买方意图类」搜索关键词的天然购买意图，给基础分
KW_BUY_BONUS = 2
KW_BUY_RE = re.compile(
    r"rfq|inquir|buyer|sourc|request for quote|looking for|seeking|"
    r"outsourc|contract manufacturing|oem|odm|need custom|buyer request|"
    r"want to source",
    re.I,
)
# 强买方意图正则（命中即视为高质量买家，额外加分并提升置信度）
BUYER_INTENT_RE = re.compile(
    r"we (?:are|'re) looking for|our company (?:needs|is looking for|requires)|"
    r"looking to source|looking to outsource|requesting (?:a )?quote|"
    r"seeking a supplier|need to source|interested in sourcing|"
    r"help (?:us|me) find a supplier",
    re.I,
)
RULE_MIN_SCORE = 3
# 注：TRUE_BUYER_RE / RFQ_PLATFORM_RE 已移至 lead_filter_engine 模块
def _company_from_result(r):
    """最佳努力从 URL 域名或标题推导公司 / 来源名（不依赖外部 API）。"""
    url = r.get("url", "") or ""
    title = (r.get("title") or "").strip()
    domain = ""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        domain = netloc.split(".")[0]
    except Exception:
        pass
    # 标题不像句子开头（how/what/why/the）时，用标题作公司线索更准确
    if title and not title.lower().startswith(("how", "what", "why", "the ")):
        return title[:50]
    if domain:
        return domain.capitalize()
    return "Unknown"


def clean_with_rules(raw_results):
    """基于 Python 内置规则的确定性清洗（无需任何 API）。

    按购买意图关键词加权打分，过滤掉新闻 / 教程 / 供应商自广告等噪声，
    即使没有 OpenAI 额度，也能稳定产出合格线索。
    """
    seen = set()
    scored = []
    for r in raw_results:
        url = r.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)

        blob = f"{r.get('title','')} {r.get('snippet','')} {r.get('keyword','')}".lower()

        # 同行 / 供应商自广告硬过滤：命中即剔除，绝不把竞争对手当线索输出
        if is_competitor(url, r.get("title", ""), r.get("snippet", "")):
            continue

        def _sum(patterns):
            return sum(w for pat, w in patterns if re.search(pat, blob))

        buy = _sum(BUY_SIGNALS)
        ad = _sum(AD_SIGNALS)
        noise = _sum(NOISE_SIGNALS)
        # 搜索关键词本身含购买意图时加基础分（搜索已定向到买家）
        kw_bonus = KW_BUY_BONUS if KW_BUY_RE.search(r.get("keyword", "")) else 0
        # 强买方意图短语：额外加分，凸显真实询盘
        buyer_intent = bool(BUYER_INTENT_RE.search(blob))
        score = (buy + _sum(DOMAIN_SIGNALS) + kw_bonus
                 + (5 if buyer_intent else 0) - ad - noise)

        # 真·买方意图闸门：必须是「买家亲口说出」的采购/寻源/外包动作，
        # 或是采购平台上的真实求购贴（RFQ / 图纸需求）；否则一律不入库，
        # 从根本上杜绝把同行供应商自广告当线索输出。
        true_buyer = bool(TRUE_BUYER_RE.search(blob))
        rfq_platform = bool(RFQ_PLATFORM_RE.search(blob))
        if (true_buyer or rfq_platform) and ad < 2 and noise == 0:
            # 命中强买方意图直接拉高置信度，确保真实买家排到前面
            base_conf = "high" if score >= 8 else ("medium" if score >= 5 else "low")
            conf = ("high" if (buyer_intent and base_conf in ("high", "medium"))
                    else base_conf)
            scored.append({
                "company": extract_company_name(r.get("title"), r.get("url"), r.get("snippet")),
                "need_summary": (r.get("snippet") or r.get("title") or "")[:160],
                "source_url": url,
                "keyword": r.get("keyword", ""),
                "confidence": conf,
                "_score": score,
            })

    scored.sort(key=lambda x: x["_score"], reverse=True)
    for s in scored:
        s.pop("_score", None)
    return scored[:RESULTS_LIMIT]


def _is_retryable(e):
    """判断异常是否值得重试：429 限流 / 超时 / 网络抖动可重试；
    4xx 其它（如 401 认证失败）重试无意义，直接跳过。"""
    status = getattr(e, "status", None)
    if status == 429:
        return True
    if status is not None and 400 <= status < 500 and status != 429:
        return False  # 4xx 客户端错误（含 401/403）不应重试
    name = type(e).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "ratelimit", "servererror")):
        return True
    txt = str(e).lower()
    if any(k in txt for k in ("429", "too many requests", "timeout", "timed out",
                              "connection", "rate limit", "emporarily")):
        return True
    return False


def clean_with_ai(raw_results):
    """优先用大模型清洗；遇 429 / 超时 / 网络错误自动指数退避重试（1-2 次），
    重试仍失败或完全无密钥时，回退到确定性的本地规则清洗，确保当天线索不丢失。"""
    api_key = cfg("OPENAI_API_KEY")

    # 1) 完全没配置密钥或 SDK 不可用 -> 直接走规则清洗
    if not api_key or OpenAI is None:
        print("[ai] 未配置 OPENAI_API_KEY 或 openai 未安装；"
              "回退到基于规则的本地清洗。", file=sys.stderr)
        return clean_with_rules(raw_results)

    # 2) 调用 OpenAI 兼容端点（base_url / model 均可通过环境变量切换）
    client = OpenAI(
        api_key=api_key,
        base_url=cfg("OPENAI_BASE_URL") or None,   # 兼容 Azure / OpenRouter 等
    )
    model = cfg("OPENAI_MODEL", "gpt-4o-mini")
    payload = json.dumps(raw_results, ensure_ascii=False, indent=2)

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "Raw search results (JSON):\n\n" + payload},
                ],
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(content)
            leads = data.get("leads", [])
            if not leads:
                print("[ai] 大模型未返回任何合格线索；回退到基于规则的本地清洗。",
                      file=sys.stderr)
                return clean_with_rules(raw_results)
            return leads[:RESULTS_LIMIT]
        except Exception as e:
            if attempt < MAX_RETRIES and _is_retryable(e):
                delay = RETRY_BASE_DELAY * (2 ** attempt)  # 指数退避：3s, 6s ...
                print(f"[ai] 调用失败 ({type(e).__name__})，{delay}s 后重试 "
                      f"({attempt + 1}/{MAX_RETRIES}) ...", file=sys.stderr)
                time.sleep(delay)
                continue
            # 不可重试 或 重试耗尽 -> 规则回退，绝不丢弃当天线索
            reason = type(e).__name__
            if getattr(e, "status", None) == 429 or "insufficient" in str(e).lower():
                reason = "429 insufficient_quota"
            print(f"[ai] 大模型清洗最终失败 ({reason})；"
                  "回退到基于规则的本地清洗，确保今日线索不丢失。", file=sys.stderr)
            return clean_with_rules(raw_results)
    # 理论不可达（循环内必然 return），仅作保险兜底
    return clean_with_rules(raw_results)


# ---------------------------------------------------------------------------
# 网页邮箱提取（Regex 轻量抓取 + 灰名单过滤）
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
# 静态资源后缀 -> 形如 image@x.png 的伪邮箱
EMAIL_STATIC_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".css", ".js",
                     ".svg", ".webp", ".ico", ".pdf")
# 占位 / 示例域名
EMAIL_JUNK_DOMAINS = ("example.com", "example.org", "example.net",
                       "test.com", "localhost", "w3.org")
# 系统 / 无回复邮箱（通常不可作为跟进线索）
EMAIL_NOREPLY_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply",
                       "mailer-daemon", "postmaster")


def _is_real_email(email):
    """判断一个字符串是否为可跟进的真实邮箱（过滤占位/静态资源/无回复）。"""
    e = (email or "").strip().lower()
    if "@" not in e:
        return False
    local, _, domain = e.rpartition("@")
    if not local or not domain:
        return False
    if domain in EMAIL_JUNK_DOMAINS:
        return False
    if any(e.endswith(ext) for ext in EMAIL_STATIC_EXTS):
        return False
    if local in EMAIL_NOREPLY_LOCAL:
        return False
    return True


def extract_emails_from_text(text):
    """从纯文本中用正则提取邮箱，去重并过滤垃圾邮箱。"""
    if not text:
        return []
    out = []
    for raw in EMAIL_RE.findall(text):
        e = raw.strip(".,;<>()[]'\" ")
        if _is_real_email(e) and e not in out:
            out.append(e)
    return out


def extract_emails_from_html(html):
    """从 HTML 中提取邮箱：同时扫描可见正文与 mailto: 链接。"""
    emails = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
        emails.update(extract_emails_from_text(soup.get_text(" ", strip=True)))
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].split(",")
                for part in addr:
                    part = part.strip()
                    if _is_real_email(part):
                        emails.add(part)
    except Exception as e:
        print(f"[email] HTML 解析失败：{e}", file=sys.stderr)
    return sorted(emails)


def fetch_page_text(url, timeout=8, max_bytes=500_000):
    """轻量抓取网页正文（限制体积，避免大文件拖慢运行）。失败返回 None。"""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return None
        data = b""
        for chunk in resp.iter_content(8192):
            data += chunk
            if len(data) >= max_bytes:
                break
        enc = resp.encoding or "utf-8"
        return data.decode(enc, errors="ignore")
    except Exception as e:
        print(f"[email] 抓取失败 {url}: {e}", file=sys.stderr)
        return None


def enrich_leads_with_emails(leads):
    """为每条线索补充提取到的邮箱（来自摘要文本 + 网页抓取），不阻断主流程。

    - 即使无网络 / 抓取失败，也会从摘要文本中尽力提取邮箱
    - 网页抓取受 EMAIL_MAX_FETCH / EMAIL_FETCH_TIMEOUT 限制，单条失败不影响其它线索
    """
    enabled = str(os.getenv("EMAIL_EXTRACTION", "1")).lower() in ("1", "true", "yes")
    timeout = int(os.getenv("EMAIL_FETCH_TIMEOUT", "8"))
    max_fetch = int(os.getenv("EMAIL_MAX_FETCH", "20"))
    if not enabled:
        print("[email] 网页抓取已关闭（EMAIL_EXTRACTION != 1），"
              "仅从摘要文本提取。", file=sys.stderr)
    fetched = 0
    for l in leads:
        found = set(extract_emails_from_text(l.get("need_summary", "") or ""))
        url = l.get("source_url")
        if enabled and url and fetched < max_fetch:
            html = fetch_page_text(url, timeout=timeout)
            if html:
                found.update(extract_emails_from_html(html))
            fetched += 1
            if found:
                print(f"[email] {url} -> {len(found)} 个邮箱", file=sys.stderr)
        l["emails"] = sorted(found)[:5]
    return leads


# ---------------------------------------------------------------------------
# 增强模块：黑名单过滤 · 公司名提取 · 意向评分(0-100) · 个性化开发信
# ---------------------------------------------------------------------------

def _domain_of(url):
    """从 URL 中提取主机名（去掉 www. 前缀）。"""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _email_domain(email):
    return email.rpartition("@")[2].lower()


# 垃圾 / 纯内容站点黑名单（知乎、维基、问答、博客、视频、资讯等）
BLACKLIST_DOMAINS = (
    "zhihu.com", "wikipedia.org", "wikihow.com", "quora.com", "medium.com",
    "pinterest.com", "youtube.com", "vimeo.com", "blogspot.com", "wordpress.com",
    "substack.com", "tumblr.com", "cnn.com", "bbc.com", "nytimes.com",
)
# 通用博客 / 资讯主机后缀
BLACKLIST_DOMAIN_SUFFIX = (".blogspot.com", ".wordpress.com", ".substack.com")
# 标题 / 摘要中的纯内容或中介广告信号（命中即视为垃圾）
BLACKLIST_KEYWORDS = (
    "how to", "what is", "what are", "guide to", "top 10", "best of", "review of",
    "vs ", " explained", "tutorial", "salary", "job opening", "hiring",
    "definition of", "freelance", "upwork", "fiverr", "affiliate", "sponsored",
)


def is_blacklisted(url, title="", snippet=""):
    """判断一条原始结果是否来自垃圾站点 / 纯内容站 / 中介广告。"""
    domain = _domain_of(url)
    if domain in BLACKLIST_DOMAINS:
        return True
    # 同时匹配子域名（如 en.wikipedia.org）
    if any(domain == bd or domain.endswith("." + bd) for bd in BLACKLIST_DOMAINS):
        return True
    if any(domain.endswith(s) for s in BLACKLIST_DOMAIN_SUFFIX):
        return True
    text = f"{title} {snippet}".lower()
    if any(k in text for k in BLACKLIST_KEYWORDS):
        return True
    return False


def filter_blacklist(raw_results):
    """过滤掉垃圾站点 / 纯内容 / 中介广告，仅保留潜在的真实买家线索。"""
    kept, dropped = [], 0
    for r in raw_results:
        if is_blacklisted(r.get("url", ""), r.get("title", ""), r.get("snippet", "")):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        print(f"[filter] 已过滤 {dropped} 条垃圾/内容站点结果。", file=sys.stderr)
    return kept



# 注：反同行（Anti-Competitor）+ 同行邮箱识别逻辑已统一移至 lead_filter_engine 模块
# 公司名提取：优先从标题拆分出机构名，失败回退到域名
def extract_company_name(title, url, snippet=""):
    cand = (title or "").strip()
    for sep in [" | ", " - ", " – ", " :: ", " — ", " · ", " » ", " > "]:
        if sep in cand:
            cand = cand.split(sep)[0].strip()
            break
    # 去掉描述性后缀词（采购意图 / 行业词），保留机构主体
    cand = re.sub(
        r"\b(rfq|inquiry|enquiry|buyer|request for quote|sourcing|supplier|"
        r"manufacturer|quote|wanted|needed|looking for)\b.*$",
        "", cand, flags=re.I,
    ).strip(" .,-|：:•")
    if cand and 2 <= len(cand) <= 60 and not cand.lower().startswith(
        ("how ", "what ", "why ", "the ", "a ", "an ", "top ", "best ")):
        return cand[:60]
    domain = _domain_of(url).split(".")[0]
    if domain and domain != "www":
        return domain.capitalize()
    return "Unknown"


# ----- 意向评分（0-100）加权项 -----
MATERIAL_PATTERNS = [
    r"aluminium|aluminum", r"zinc", r"steel|stainless", r"magnesium",
    r"titanium", r"brass|bronze", r"plastic|abs| nylon|peek|polymer", r"copper",
]
PARAM_PATTERNS = [
    r"tolerance", r"micron|µm", r"\bmm\b|\binch\b", r"surface finish",
    r"anodiz", r"heat treat", r"thread", r"hardness", r"gd&t",
]
DRAWING_PATTERNS = [
    r"drawing", r"\bcad\b", r"step file|iges|dxf|\bstp\b", r"blueprint",
    r"3d model", r"technical spec",
]
QTY_PATTERNS = [
    r"\bpcs\b|pieces|units", r"\bmoq\b|minimum order", r"\bbatch\b",
    r"\d{2,3}\s?(pcs|pieces|units|k)\b", r"quantity|volume|annual demand",
]
# 高价值来源（垂直黄页 / 专业社区）加分
HIGH_QUALITY_DOMAINS = (
    set(DIRECTORY_SITES)
    | {"thomasnet.com", "kompass.com", "globalspec.com", "engineering.com"}
)


def score_lead(lead):
    """0-100 意向评分：基础分(置信度) + 真实企业邮箱 + 具体工艺/材质/图纸/数量。"""
    score = {"high": 40, "medium": 25, "low": 10}.get(
        (lead.get("confidence") or "low").lower(), 10)
    emails = lead.get("emails") or []
    if emails:
        score += 20
        # 至少一个非免费邮箱（企业域名）再加分
        if any(_email_domain(e) not in FREE_EMAIL_DOMAINS for e in emails if "@" in e):
            score += 10
    text = " ".join([
        lead.get("need_summary", ""), lead.get("keyword", ""),
        lead.get("source_url", ""),
    ]).lower()
    if any(re.search(p, text) for p in MATERIAL_PATTERNS):
        score += 8
    if any(re.search(p, text) for p in PARAM_PATTERNS):
        score += 8
    if any(re.search(p, text) for p in DRAWING_PATTERNS):
        score += 8
    if any(re.search(p, text) for p in QTY_PATTERNS):
        score += 8
    url = (lead.get("source_url") or "").lower()
    if any(d in url for d in HIGH_QUALITY_DOMAINS):
        score += 10
    return max(0, min(100, score))


def tier_from_score(score):
    """根据分数返回 (中文标签, 样式类)。"""
    if score >= 70:
        return ("🔥 高意向", "hot")
    if score >= 45:
        return ("⚡ 中意向", "mid")
    return ("💤 低意向", "low")


def matched_capabilities(text):
    """根据线索文本匹配我们对应的制造能力，用于开发信个性化。"""
    t = (text or "").lower()
    caps = []
    if re.search(r"\bcnc\b|machin|milling|turning|5[- ]?axis", t):
        caps.append("CNC machining (3/5-axis milling & turning)")
    if re.search(r"die[- ]?cast|aluminium? casting|zinc casting|magnesium", t):
        caps.append("aluminum / zinc die casting")
    if re.search(r"\bcast|sand cast|gravity cast", t):
        caps.append("sand & gravity casting")
    if re.search(r"injection|mold|mould|plastic part", t):
        caps.append("plastic injection molding & tooling")
    if re.search(r"stamp|fabricat|sheet metal", t):
        caps.append("metal stamping & fabrication")
    return caps


def generate_cold_email(lead):
    """为合格线索生成简短、专业的英文破冰开发信（模板，无需 API，稳定可靠）。"""
    company = (lead.get("company") or "there").strip()
    if company.lower() in ("unknown", "", "there"):
        company = "there"
    summary = (lead.get("need_summary") or "").strip()
    keyword = lead.get("keyword", "")
    caps = matched_capabilities(f"{summary} {keyword}")
    cap_sentence = (
        ", ".join(caps) if caps else
        "CNC machining, aluminum die casting, and plastic injection molding"
    )
    ref = summary if summary else f"your recent '{keyword}' sourcing request"
    return (
        f"Subject: Precision manufacturing support for {company}\n\n"
        f"Hi {company} team,\n\n"
        f"I came across {ref} and wanted to introduce AlumCasting as a "
        f"potential manufacturing partner. We specialize in {cap_sentence}, "
        f"with in-house tooling, strict tolerance control, and flexible "
        f"low-to-high volume production.\n\n"
        f"If you're still evaluating suppliers, I'd be glad to share our "
        f"capability portfolio, similar-project references, and a competitive "
        f"quote. Feel free to reply with your drawing or requirements.\n\n"
        f"Best regards,\n"
        f"Hank\n"
        f"AlumCasting — Precision Manufacturing\n"
        f"Email: alumcastor@gmail.com"
    )


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_html_report(leads, generated_at):
    if not leads:
        body = ('<p class="empty">今天没有匹配到新的潜在客户线索'
                '（可能是搜索结果较少、AI 过滤未通过，或均为已推送过的重复线索）。'
                '明天继续监控。</p>')
    else:
        cards = []
        for i, l in enumerate(leads, 1):
            score = int(l.get("score", 0) or 0)
            tier_label, tier_cls = tier_from_score(score)
            badge = f'<span class="badge {tier_cls}">{tier_label}</span>'
            score_span = f'<span class="score">意向分 {score}</span>'
            kw = esc(l.get("keyword", ""))
            kw_tag = f'<span class="tag">#{kw}</span>' if kw else ""
            url = l.get("source_url", "") or "#"
            company = esc(l.get("company", "Unknown"))
            summary = esc(l.get("need_summary", "")) or "（无摘要）"
            emails = l.get("emails") or []
            if emails:
                email_links = " · ".join(
                    f'<a class="email" href="mailto:{esc(e)}">{esc(e)}</a>'
                    for e in emails[:3]
                )
                email_block = f'<p class="lead-email">✉️ 邮箱: {email_links}</p>'
            else:
                email_block = '<p class="lead-email none">✉️ 邮箱: 未公开</p>'
            cold = l.get("cold_email") or ""
            cold_block = (
                '<details class="cold"><summary>✍️ 英文开发信草稿（点击展开/复制）</summary>'
                f'<pre>{esc(cold)}</pre></details>'
            ) if cold else ""
            cards.append(f"""
            <div class="lead">
              <div class="lead-top">
                <span class="idx">#{i}</span>
                {badge}
                {score_span}
                {kw_tag}
              </div>
              <a class="lead-title" href="{esc(url)}" target="_blank" rel="noopener">{company}</a>
              <p class="lead-summary">{summary}</p>
              {email_block}
              {cold_block}
              <a class="lead-link" href="{esc(url)}" target="_blank" rel="noopener">查看原始来源 ↗</a>
            </div>""")
        body = "".join(cards)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日潜在客户线索日报</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; background:#f4f6f9; margin:0; color:#1f2d3d; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 24px 16px; }}
  .header {{ background: linear-gradient(135deg,#0d4a8e,#1e88e5); color:#fff; border-radius:14px; padding:22px 24px; }}
  .header h1 {{ margin:0 0 6px; font-size:20px; }}
  .header p {{ margin:0; opacity:.92; font-size:13px; line-height:1.5; }}
  .card {{ background:#fff; border-radius:14px; padding:18px; margin-top:16px; box-shadow:0 2px 10px rgba(0,0,0,.05); }}
  .lead {{ border:1px solid #eef1f5; border-radius:12px; padding:14px 16px; margin-bottom:12px; background:#fcfdfe; }}
  .lead:last-child {{ margin-bottom:0; }}
  .lead-top {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:8px; }}
  .idx {{ color:#9aa7b4; font-size:13px; font-weight:600; }}
  .tag {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; background:#eef4fb; color:#2b6cb0; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:700; }}
  .badge.high {{ background:#fdecea; color:#c0392b; }}
  .badge.medium {{ background:#fff4e0; color:#b9770e; }}
  .badge.low {{ background:#eef2f6; color:#7a8794; }}
  .score {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; background:#eef4fb; color:#2b6cb0; }}
  .lead-title {{ display:block; font-size:16px; font-weight:700; color:#15233a; text-decoration:none; line-height:1.35; }}
  .lead-title:hover {{ color:#1e88e5; }}
  .lead-summary {{ margin:8px 0 10px; font-size:14px; line-height:1.6; color:#415062; }}
  .lead-link {{ display:inline-block; font-size:13px; color:#1e88e5; text-decoration:none; font-weight:600; }}
  .lead-link:hover {{ text-decoration:underline; }}
  .lead-email {{ margin:0 0 10px; font-size:13px; color:#415062; word-break:break-all; }}
  .lead-email .email {{ color:#c0392b; text-decoration:none; font-weight:600; }}
  .lead-email .email:hover {{ text-decoration:underline; }}
  .lead-email.none {{ color:#9aa7b4; font-style:italic; }}
  .cold {{ margin:10px 0; border:1px dashed #cfd8e3; border-radius:10px; padding:8px 12px; background:#fbfcfe; }}
  .cold summary {{ cursor:pointer; font-size:13px; font-weight:600; color:#2b6cb0; user-select:none; }}
  .cold pre {{ white-space:pre-wrap; word-break:break-word; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:12.5px; line-height:1.55; color:#2c3e50; margin:8px 0 2px; }}
  .empty {{ color:#7a8794; padding:14px 0; line-height:1.6; }}
  .footer {{ text-align:center; color:#9aa7b4; font-size:12px; margin-top:18px; line-height:1.6; }}
  @media (max-width:480px) {{
    .wrap {{ padding:14px 10px; }}
    .lead-title {{ font-size:15px; }}
    .lead-summary {{ font-size:13px; }}
  }}
</style></head>
<body><div class="wrap">
  <div class="header">
    <h1>🔧 每日潜在客户线索日报</h1>
    <p>垂直行业：CNC 加工 · 压铸 (Die Casting) · 铸造 (Casting) · 塑胶模具与注塑</p>
    <p>生成时间：{generated_at} · 共 {len(leads)} 条新线索</p>
  </div>
  <div class="card">{body}</div>
  <div class="footer">本邮件由 GitHub Actions 自动生成 · 仅供商务拓展参考<br>重复线索已按历史记录自动过滤</div>
</div></body></html>"""


# ---------------------------------------------------------------------------
# 历史去重（A1：TTL 生命周期去重，替代原永久去重）
# ---------------------------------------------------------------------------

def _history_key(url):
    """去重键：以线索原始 URL 为稳定键（与历史格式兼容），仅做空白归一化。

    刻意不复用其它易碰撞的键——原系统即以 source_url 作为去重键，保持兼容。
    """
    return (url or "").strip()


def _now_ts():
    return time.time()


def _entry_is_expired(entry, now, ttl_days):
    """判断一条缓存记录是否已过 TTL（以 last_seen 为基准）。

    边界约定：age >= TTL 即视为过期（允许重新发现）。
    """
    try:
        last_seen = float(entry.get("last_seen", 0))
    except (TypeError, ValueError):
        return True
    return (now - last_seen) >= ttl_days * 86400


def prune_expired_entries(entries, ttl_days=DISCOVERY_DEDUP_TTL_DAYS, now=None):
    """剔除超过 TTL 的缓存记录，返回仅含存活记录的 dict。

    过期记录会被清理，从而不再阻塞对应线索重新进入 pipeline。
    非 dict / 缺 last_seen / 非法时间戳的条目一律丢弃（容错，不崩溃）。
    """
    now = now if now is not None else _now_ts()
    kept = {}
    for k, v in entries.items():
        if not isinstance(v, dict) or "last_seen" not in v:
            continue
        if _entry_is_expired(v, now, ttl_days):
            continue
        kept[k] = v
    return kept


def _migrate_legacy(data, now):
    """把旧版缓存（list / {"urls": [...]}）迁移为 TTL 感知 entry dict。

    旧 entry 视为 last_seen = 迁移时刻（reference time），使其获得一个全新的
    TTL 窗口，而非立即过期或永久阻塞——既不破坏旧格式，也不清空整个缓存。
    """
    urls = []
    if isinstance(data, list):
        urls = data
    elif isinstance(data, dict):
        urls = data.get("urls", []) or []
    entries = {}
    for u in urls:
        if not isinstance(u, str):
            continue
        k = _history_key(u)
        if k:
            entries[k] = {"first_seen": now, "last_seen": now}
    return entries


def load_sent_history(path=HISTORY_FILE, ttl_days=DISCOVERY_DEDUP_TTL_DAYS):
    """读取 TTL 感知的去重缓存，返回 {key: {"first_seen": ts, "last_seen": ts}}。

    处理策略：
    * 文件缺失 / 损坏 / 非法 JSON -> 返回空 dict（绝不崩溃）。
    * 新版格式 {"version": 2, "entries": {...}} -> 解析并容错非法 entry。
    * 旧版格式 list / {"urls": [...]} -> 迁移为 entry dict（last_seen=读取时刻）。
    * 加载后即时裁剪过期 entry，使内存中的缓存只含存活记录。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return {}
    if isinstance(data, dict) and "entries" in data:
        entries = {}
        for k, v in data["entries"].items():
            if not isinstance(v, dict) or "last_seen" not in v:
                continue  # 非法 entry -> 忽略
            try:
                fs = float(v.get("first_seen", v["last_seen"]))
                ls = float(v["last_seen"])
            except (TypeError, ValueError):
                continue
            entries[k] = {"first_seen": fs, "last_seen": ls}
        return prune_expired_entries(entries, ttl_days)
    # 旧版格式
    return _migrate_legacy(data, _now_ts())


def save_sent_history(urls, path=HISTORY_FILE, max_len=HISTORY_MAX,
                      ttl_days=DISCOVERY_DEDUP_TTL_DAYS, now=None):
    """将本次成功推送的 URL 合并写入 TTL 感知缓存。

    * 新键：first_seen = now；已存在键：仅刷新 last_seen = now（保留首次发现时间）。
    * 写入前再次裁剪过期 entry + 受 HISTORY_MAX 上限约束（按 last_seen 最旧优先淘汰）。
    * 落盘格式：{"version": 2, "ttl_days": ..., "entries": {...}}。
    * 旧 list 格式在首次写入后自然升级为新格式，不丢失、不清空历史。
    """
    now = now if now is not None else _now_ts()
    urls = [_history_key(u) for u in urls if u]
    if not urls:
        return
    entries = load_sent_history(path, ttl_days)  # 已裁剪为存活记录
    for k in urls:
        if k in entries:
            entries[k]["last_seen"] = now
        else:
            entries[k] = {"first_seen": now, "last_seen": now}
    # 防御性再裁剪过期项
    entries = prune_expired_entries(entries, ttl_days, now)
    # 尺寸保护：超出上限时保留 last_seen 最近的前 max_len 条
    if len(entries) > max_len:
        ordered = sorted(entries.items(), key=lambda kv: kv[1]["last_seen"])
        entries = dict(ordered[-max_len:])
    try:
        payload = {"version": 2, "ttl_days": ttl_days, "entries": entries}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[history] 写入缓存失败：{e}", file=sys.stderr)


def dedupe_leads(leads, history, ttl_days=DISCOVERY_DEDUP_TTL_DAYS, now=None):
    """过滤掉去重缓存中仍存活（未过期）的线索；无 URL 的线索不做去重。

    `history` 接受两种形态（向后兼容）：
    * TTL 感知 dict（推荐）：先裁剪过期 entry 再判定，过期键允许重新进入。
    * 旧版 set / list：无时间戳，全部视为存活（维持原行为）。
    """
    now = now if now is not None else _now_ts()
    if isinstance(history, dict):
        live = prune_expired_entries(history, ttl_days, now)
        live_keys = set(live.keys())
    else:
        live_keys = set(history)  # 旧版 set/list -> 全部存活
    kept, dropped = [], 0
    for l in leads:
        key = _history_key(l.get("source_url"))
        if key and key in live_keys:
            dropped += 1
            continue
        kept.append(l)
    if dropped:
        print(f"[dedup] 已过滤 {dropped} 条历史重复线索。", file=sys.stderr)
    return kept


# ---------------------------------------------------------------------------
# 邮件发送
# ---------------------------------------------------------------------------

def send_email(subject, html):
    server = _first_set(MAIL_HOST_ALIASES)
    port = int(cfg("MAIL_PORT", "465"))
    username = _first_set(MAIL_USER_ALIASES)
    password = cfg("MAIL_PASSWORD")
    recipient = cfg("MAIL_RECIPIENT", "alumcastor@gmail.com")

    # Gmail（及多数邮件服务商）要求信封/信件 From 必须等于认证账户本身，
    # 否则会报 5.7.1 SendAsDenied（地址不匹配）。因此发件人默认对齐到登录账号；
    # 若显式配置了 MAIL_SENDER，仅当它等于登录账号时才生效，否则告警并回退到
    # 登录账号，避免 GitHub Actions 用 Gmail 应用专用密码发信时出现校验/发送报错。
    sender_env = cfg("MAIL_SENDER")
    sender = (sender_env or username or "").strip()
    if sender_env and username and sender_env.strip().lower() != username.strip().lower():
        print("[mail][WARN] MAIL_SENDER 与登录账号不一致；Gmail 会强制改写为登录账号，"
              "已自动对齐到发件账号以避免 SendAsDenied 错误。", file=sys.stderr)
        sender = username

    if not (server and username and password):
        print("[mail] SMTP 凭据不完整；跳过发送，仅本地保存 HTML。",
              file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("AlumCasting Leads", sender)) if sender else ""
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    # 端口 465 使用隐式 SSL；其它端口（如 587）使用 STARTTLS 显式加密升级。
    # SiteGround 企业邮（sgp14.siteground.asia）推荐用 587 + STARTTLS。
    use_ssl = (port == 465)
    timeout = 30

    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(server, port, context=context, timeout=timeout) as s:
                s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(server, port, timeout=timeout) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())  # 加密升级
                s.ehlo()
                s.login(username, password)
                s.send_message(msg)
        print(f"[mail] 日报已发送至 {recipient}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[mail][ERROR] SMTP 认证失败 (SMTPAuthenticationError)："
              f"用户名或密码/授权码错误。详情: {e}", file=sys.stderr)
    except smtplib.SMTPConnectError as e:
        print(f"[mail][ERROR] 无法连接 SMTP 服务器 (SMTPConnectError)："
              f"地址或端口错误，或网络不通。详情: {e}", file=sys.stderr)
    except smtplib.SMTPServerDisconnected as e:
        print(f"[mail][ERROR] SMTP 服务器在通信过程中断开 (SMTPServerDisconnected)："
              f"{e}", file=sys.stderr)
    except smtplib.SMTPException as e:
        print(f"[mail][ERROR] SMTP 协议错误 (SMTPException)：{e}", file=sys.stderr)
    except socket.timeout:
        print("[mail][ERROR] 连接超时 (socket.timeout)：SMTP 服务器响应过慢或网络受限，"
              "请检查 MAIL_SERVER / MAIL_PORT。", file=sys.stderr)
    except OSError as e:
        print(f"[mail][ERROR] 网络/系统底层错误 (OSError)：{e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[mail][ERROR] 发送邮件时发生未知错误：{e!r}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# B4 — 供应商目录 Listing 过滤（针对 site: 定向查询到的黄页 / 社区）
# ---------------------------------------------------------------------------
def _domain_of(url):
    """从 URL 中提取主机名（小写）。"""
    m = re.search(r"https?://([^/]+)/?", url or "")
    return (m.group(1).lower() if m else "")


def is_directory_listing_lead(url, title, snippet):
    """判断一条目录站结果是否是「纯供应商 Listing、无买方意图」。

    规则（B4）：仅当来源域名属于 DIRECTORY_SITES（Thomasnet / Kompass /
    GlobalSpec / Engineering 等）**且**文本缺乏真实买方意图时，才判定为
    纯目录 Listing 并剔除。若目录页面带有 RFQ / 寻源 / 采购信号，则放行，
    绝不对整个域名一刀切（避免误杀目录里的真实 Buyer / RFQ 贴）。

    注意：此函数只针对目录站做「listing 级」额外过滤；同行自广告与普通
    供应商仍由 is_competitor 处理，买方意图仍由 passes_buyer_gate 把关。

    Args:
        url (str):    结果 URL。
        title (str):  结果标题。
        snippet (str):结果摘要。

    Returns:
        bool: True 表示「目录站纯 Listing、无买方意图」，应剔除。
    """
    dom = _domain_of(url)
    if not any(d in dom for d in DIRECTORY_SITES):
        return False
    text = f"{title} {snippet}".lower()
    # 目录站 + 无买方意图 -> 视为纯供应商目录页（Supplier profile / Product listing）
    return not passes_buyer_gate(text)


def filter_directory_listings(raw_results):
    """过滤掉目录站上「无买方意图」的纯 Listing 结果，仅保留带采购信号的页面。"""
    kept, dropped = [], 0
    for r in raw_results:
        if is_directory_listing_lead(r.get("url", ""), r.get("title", ""),
                                     r.get("snippet", "")):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        print(f"[filter] 已过滤 {dropped} 条目录站纯 Listing（无买方意图）。",
              file=sys.stderr)
    return kept


# ---------------------------------------------------------------------------
# B2 — AI 清洗后强制再跑确定性闸门（LLM 分类只是建议，闸门才是权威）
# ---------------------------------------------------------------------------
def _lead_text_blob(lead):
    """拼接一条线索用于确定性判定的文本（用于 AI 返回后的再校验）。"""
    return " ".join(filter(None, [
        lead.get("need_summary", ""),
        lead.get("keyword", ""),
        lead.get("source_url", ""),
        lead.get("company", ""),
    ])).lower()


def apply_post_ai_gates(leads):
    """clean_with_ai() 返回后强制再执行确定性闸门（B2 最高优先级）：

    1. filter_competitors  —— 同行 / 供应商自广告硬过滤（AI 误判为 buyer 也拦得住）；
    2. passes_buyer_gate   —— 买方意图闸门（缺买方动作则剔除）；
    3. classify_company    —— 附加 7 类公司角色标签（仅辅助，不改变入库判定）。

    核心原则：**AI 不能绕过 competitor gate，也不能绕过 buyer gate。** 即使 AI
    标注为 BUYER，仍必须再次通过两道确定性闸门才能进入最终日报。

    Returns:
        (kept, comp_drop, buyer_drop): 通过闸门的线索 + 两道闸门各自剔除数。
    """
    kept, comp_drop, buyer_drop = [], 0, 0
    for l in leads:
        # 同行闸门：只用线索「实际内容」（need_summary + url）判定，
        # 不把搜索关键词 keyword 计入——keyword 本质是买方意图查询
        # （如 "seeking CNC machining supplier"），若计入会把真买家误判为同行。
        if is_competitor(l.get("source_url", ""), l.get("need_summary", ""), ""):
            comp_drop += 1
            continue
        # 买方闸门：用拼接文本复查
        if not passes_buyer_gate(_lead_text_blob(l)):
            buyer_drop += 1
            continue
        l["_company_class"] = classify_company(l)
        kept.append(l)
    if comp_drop:
        print(f"[post-ai] 已剔除 {comp_drop} 条 AI 误判的同行/供应商线索。",
              file=sys.stderr)
    if buyer_drop:
        print(f"[post-ai] 已剔除 {buyer_drop} 条 AI 误判但缺买方意图的线索。",
              file=sys.stderr)
    return kept, comp_drop, buyer_drop


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    # 启动期校验：配置缺失 / 无效时尽早失败并给出可读信息（不泄露 secret 值）
    try:
        validate_config()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    generated_at = now.strftime("%Y-%m-%d %H:%M (GMT+8)")

    print("==> 收集原始线索 ...")
    raw = collect_raw_leads()
    raw_results = len(raw)
    print(f"==> 共收集到 {raw_results} 条原始结果。")

    # 黑名单过滤：剔除知乎/维基/博客/中介广告等垃圾站点，只留真实买家
    n0 = len(raw)
    raw = filter_blacklist(raw)
    blacklist_rejected = n0 - len(raw)
    # 同行过滤：剔除压铸/加工/注塑等同行业工厂的自广告页面（竞争对手，非买家）
    n0 = len(raw)
    raw = filter_competitors(raw)
    competitor_raw_rejected = n0 - len(raw)
    # B4：目录站纯 Listing 过滤（site: 定向查询命中的黄页/社区，无买方意图即剔除）
    n0 = len(raw)
    raw = filter_directory_listings(raw)
    directory_rejected = n0 - len(raw)
    unique_candidates = len(raw)
    print(f"==> 过滤后剩余 {unique_candidates} 条待清洗原始结果。")

    print("==> 调用 AI 清洗与过滤 ...")
    n0 = len(raw)
    leads = clean_with_ai(raw)
    ai_rejected = n0 - len(leads)
    print(f"==> AI 清洗后剩余 {len(leads)} 条线索。")

    # B2：AI 分类只是建议，确定性闸门仍是权威。clean_with_ai 返回后必须再次执行
    # 同行闸门 + 买方闸门，AI 无法绕过任何一道确定性 gate。
    candidates = leads
    leads, comp_drop, buyer_drop = apply_post_ai_gates(leads)
    competitor_post_ai_rejected = comp_drop
    buyer_gate_rejected = buyer_drop
    print(f"==> 后闸门过滤：剔除同行 {comp_drop} 条、缺买方意图 {buyer_drop} 条；"
          f"剩余 {len(leads)} 条合格线索。")

    # A2.2-5 兜底策略：严格过滤后为 0 条时，从候选中回收 B 类（待观察）线索，
    # 避免每日日报完全空白（中 / 低意向但含强 RFQ / 寻源信号的页面值得人工二次确认）。
    n_strict = len(leads)
    if n_strict == 0:
        watch = recover_watch_leads(candidates)
        for l in watch:
            l["_watch"] = True
        leads = watch
        print(f"==> [兜底] 严格过滤为 0 条，回收 {len(watch)} 条 B 类（待观察）线索。")
    watch_recovered = len(leads) - n_strict

    # 历史去重：仅推送未发送过的新线索
    n0 = len(leads)
    history = load_sent_history()
    leads = dedupe_leads(leads, history)
    dedup_rejected = n0 - len(leads)
    print(f"==> 去重后剩余 {len(leads)} 条新线索待推送。")

    # 网页邮箱提取：为每条线索抓取并解析其来源页面中的联系邮箱
    leads = enrich_leads_with_emails(leads)
    # 同行邮箱过滤：剔除同行供应商联络邮箱；若整页邮箱全是同行则丢弃该线索
    leads = filter_competitor_emails(leads)
    total_emails = sum(len(l.get("emails") or []) for l in leads)
    print(f"==> 邮箱提取完成：共从 {len(leads)} 条线索中解析到 {total_emails} 个邮箱。")

    # 意向评分(0-100) + 个性化英文开发信，并按分数从高到低排序
    for l in leads:
        if (l.get("company") or "Unknown") in ("Unknown", "", None):
            l["company"] = extract_company_name("", l.get("source_url", ""))
        l["score"] = score_lead(l)
        # A2.2-5：B 类（待观察）线索降权，避免与高意向 A 类混淆
        if l.get("_watch"):
            l["score"] = min(l["score"], 35)
        l["cold_email"] = generate_cold_email(l)
    leads.sort(key=lambda x: x.get("score", 0), reverse=True)
    print(f"==> 已为 {len(leads)} 条线索评分并生成开发信，按意向分排序完成。")

    html = build_html_report(leads, generated_at)

    # 始终保存产物，方便在 Actions 运行记录中查看
    with open("leads_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("leads_report.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "leads": leads},
                  f, ensure_ascii=False, indent=2)

    # A2.2-1 — 安全实验开关：dry-run 下完全跳过邮件发送与历史缓存写入，
    # 不触碰 SMTP、不污染 sent_cache，但仍生成完整报告与 funnel 指标。
    if DISCOVERY_DRY_RUN:
        print("==> [dry-run] DISCOVERY_DRY_RUN=true：跳过邮件发送，"
              "不写入历史缓存（sent_cache 保持不变）。", file=sys.stderr)
        sent = False
    else:
        subject = f"每日潜在客户线索日报 · {now.strftime('%Y-%m-%d')} · {len(leads)} 条新线索"
        sent = send_email(subject, html)
        # 推送成功后才将本次线索写入历史缓存，确保不重复推送
        if sent:
            save_sent_history([l.get("source_url") for l in leads])
            print("==> 完成，历史缓存已更新。")
        else:
            print("==> 邮件发送未成功，本次线索不写入历史缓存（下次运行将重试）。",
                  file=sys.stderr)

    # A2.2-1 — Funnel 指标（每次运行都落盘，供 Week vs Month 安全实验对比）。
    # 仅做计数汇总，不修改任何 Discovery 策略（score / 阈值 / 关键词等不变）。
    metrics = {
        "generated_at": generated_at,
        "dry_run": DISCOVERY_DRY_RUN,
        "freshness": DISCOVERY_SEARCH_FRESHNESS,
        "funnel": {
            "raw_results": raw_results,
            "unique_candidates": unique_candidates,
            "blacklist_rejected": blacklist_rejected,
            "competitor_rejected": (
                competitor_raw_rejected + competitor_post_ai_rejected),
            "directory_rejected": directory_rejected,
            "ai_rejected": ai_rejected,
            "buyer_gate_rejected": buyer_gate_rejected,
            "dedup_rejected": dedup_rejected,
            "final_qualified": len(leads),
            "watch_recovered": watch_recovered,
            "emails_extracted": total_emails,
            "sent": 1 if sent else 0,
        },
    }
    with open("discovery_metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
