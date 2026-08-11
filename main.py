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
     —— 关键词采用分类矩阵（压铸/模具、CNC/精密加工、外贸/代工买家），
        支持 SEARCH_COMBINE 组合长尾查询，单次覆盖更多维度与原始线索
  2. 优先调用大模型 API 清洗、过滤垃圾信息；遇 429 / 超时 / 无额度时自动
     指数退避重试，并回退到本地规则清洗，确保不丢线索
  3. 网页邮箱提取：对每条线索的来源页面用正则抓取并过滤真实联系邮箱
     （过滤静态资源后缀、example.com 占位、no-reply 等垃圾邮箱）
  4. 生成美观、响应式的 HTML 日报（含意向评级、可点击来源、✉️ 邮箱），
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
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ---------------------------------------------------------------------------
# 配置（全部来自环境变量 / GitHub Secrets）
# ---------------------------------------------------------------------------

# 目标行业关键词矩阵（分门别类，可自行增减）。单次运行会遍历全部关键词，
# 大幅提升覆盖维度与原始线索数量。
KEYWORD_GROUPS = {
    "压铸与模具类": [
        "aluminum die casting RFQ",
        "zinc die casting supplier inquiry",
        "custom plastic mold RFQ",
        "injection molding buyer request",
        "die casting parts buyer",
        "die casting tooling RFQ",
        "magnesium die casting inquiry",
        "plastic injection mold maker wanted",
        "mold maker RFQ China",
        "die casting company looking for supplier",
    ],
    "CNC与精密加工类": [
        "CNC machining parts RFQ",
        "precision machining buyer inquiry",
        "custom metal fabrication sourcing",
        "OEM CNC milling RFQ",
        "5 axis CNC machining RFQ",
        "CNC turning parts buyer",
        "machined aluminum parts inquiry",
        "precision components sourcing agent",
        "CNC machining contract manufacturer",
        "rapid prototyping machining RFQ",
    ],
    "外贸与代工买家类": [
        "looking for manufacturing factory China",
        "contract manufacturing RFQ",
        "metal parts sourcing agent buyer",
        "OEM ODM supplier inquiry",
        "outsource manufacturing RFQ",
        "find supplier for metal parts",
        "manufacturing partner wanted",
        "buyer seeking factory CNC",
        "import metal components inquiry",
        "distributor looking for manufacturer",
    ],
}

# 组合搜索：将「工艺词」与「采购意图词」交叉，生成更多长尾查询
# （默认关闭，通过 SEARCH_COMBINE=1 开启；组合数量受 SEARCH_COMBINE_MAX 限制）
COMBINE_PROCESS = [
    "aluminum die casting", "zinc die casting", "CNC machining",
    "plastic injection molding", "precision machining", "metal stamping",
]
COMBINE_INTENT = [
    "RFQ", "buyer inquiry", "supplier wanted", "sourcing request",
    "OEM ODM inquiry", "contract manufacturer wanted",
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
        "freshness": "Week",          # 只抓近期内容，更可能匹配近期 RFQ
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


def collect_raw_leads():
    bing_key = cfg("BING_API_KEY")
    keywords = get_search_keywords()
    print(f"[search] 共 {len(keywords)} 个检索关键词。")
    results = []
    for kw in keywords:
        print(f"[search] '{kw}'")
        try:
            if bing_key:
                results.extend(bing_search(kw, bing_key))
            else:
                results.extend(ddg_search(kw))
        except Exception as e:
            print(f"[search] error on '{kw}': {e}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# AI 清洗 / 过滤
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a meticulous B2B lead-qualification analyst for a precision "
    "manufacturing company (CNC machining, aluminum die casting, sand/gravity "
    "casting, and plastic injection molding). You receive raw search results "
    "that may include RFQs, buyer inquiries, sourcing posts, marketplace "
    "listings, and a lot of irrelevant noise (news, definitions, tutorials, "
    "supplier self-advertising, job posts). "
    "Filter STRICTLY to items that look like REAL buying intent from a "
    "potential customer/buyer (a company or individual actively seeking quotes, "
    "suppliers, or custom parts). Discard pure suppliers advertising themselves, "
    "generic articles, and spam. "
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
# 购买意图信号（强）
BUY_SIGNALS = [
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
# 供应商自广告（减分）
AD_SIGNALS = [
    (r"we (are|provide|offer|supply)\b", 1),
    (r"leading (supplier|manufacturer)", 1),
    (r"our company|contact us|get a quote", 1),
]
# 纯噪声（新闻 / 教程 / 招聘）
NOISE_SIGNALS = [
    (r"news|article|tutorial|how[- ]to|wiki|definition", 2),
    (r"job|salary|career|hiring|vacancy", 2),
]
# 来自「RFQ / 询价类」搜索关键词的天然购买意图，给基础分
KW_BUY_BONUS = 2
KW_BUY_RE = re.compile(r"rfq|inquir|buyer|sourc|request for quote", re.I)
RULE_MIN_SCORE = 3


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

        def _sum(patterns):
            return sum(w for pat, w in patterns if re.search(pat, blob))

        buy = _sum(BUY_SIGNALS)
        ad = _sum(AD_SIGNALS)
        noise = _sum(NOISE_SIGNALS)
        # 搜索关键词本身含购买意图时加基础分（搜索已定向到买家）
        kw_bonus = KW_BUY_BONUS if KW_BUY_RE.search(r.get("keyword", "")) else 0
        score = buy + _sum(DOMAIN_SIGNALS) + kw_bonus - ad - noise

        # 判定合格：有购买意图、非供应商自广告、非纯噪声
        if buy >= 2 and ad < 2 and noise == 0 and score >= RULE_MIN_SCORE:
            conf = "high" if score >= 8 else ("medium" if score >= 5 else "low")
            scored.append({
                "company": _company_from_result(r),
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
            conf = (l.get("confidence") or "low").lower()
            conf_cn = {"high": "高意向", "medium": "中等意向", "low": "低意向"}.get(conf, "低意向")
            badge = f'<span class="badge {conf}">{conf_cn}</span>'
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
            cards.append(f"""
            <div class="lead">
              <div class="lead-top">
                <span class="idx">#{i}</span>
                {badge}
                {kw_tag}
              </div>
              <a class="lead-title" href="{esc(url)}" target="_blank" rel="noopener">{company}</a>
              <p class="lead-summary">{summary}</p>
              {email_block}
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
  .badge.high {{ background:#e6f7ec; color:#1a8a4f; }}
  .badge.medium {{ background:#fff4e0; color:#b9770e; }}
  .badge.low {{ background:#f0f2f5; color:#7a8794; }}
  .lead-title {{ display:block; font-size:16px; font-weight:700; color:#15233a; text-decoration:none; line-height:1.35; }}
  .lead-title:hover {{ color:#1e88e5; }}
  .lead-summary {{ margin:8px 0 10px; font-size:14px; line-height:1.6; color:#415062; }}
  .lead-link {{ display:inline-block; font-size:13px; color:#1e88e5; text-decoration:none; font-weight:600; }}
  .lead-link:hover {{ text-decoration:underline; }}
  .lead-email {{ margin:0 0 10px; font-size:13px; color:#415062; word-break:break-all; }}
  .lead-email .email {{ color:#c0392b; text-decoration:none; font-weight:600; }}
  .lead-email .email:hover {{ text-decoration:underline; }}
  .lead-email.none {{ color:#9aa7b4; font-style:italic; }}
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
# 历史去重（防止重复推送同一线索）
# ---------------------------------------------------------------------------

def load_sent_history(path=HISTORY_FILE):
    """读取已推送线索 URL 集合；文件缺失 / 损坏时返回空集合。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return set()
    if isinstance(data, list):
        return set(data)
    if isinstance(data, dict):
        return set(data.get("urls", []))
    return set()


def save_sent_history(urls, path=HISTORY_FILE, max_len=HISTORY_MAX):
    """将本次新推送的 URL 合并写入缓存；超出上限时仅保留最近 max_len 条。"""
    urls = [u for u in urls if u]
    if not urls:
        return
    existing = load_sent_history(path)
    existing.update(urls)
    if len(existing) > max_len:
        existing = set(list(existing)[-max_len:])
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(existing), f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[history] 写入缓存失败：{e}", file=sys.stderr)


def dedupe_leads(leads, history):
    """过滤掉历史已推送过的线索；无 URL 的线索不做去重（始终视为新）。"""
    kept, dropped = [], 0
    for l in leads:
        url = l.get("source_url")
        if url and url in history:
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
    recipient = cfg("MAIL_RECIPIENT", "Hank@alumcasting.com")
    sender = cfg("MAIL_SENDER") or username

    if not (server and username and password):
        print("[mail] SMTP 凭据不完整；跳过发送，仅本地保存 HTML。",
              file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
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
    print(f"==> 共收集到 {len(raw)} 条原始结果。")

    print("==> 调用 AI 清洗与过滤 ...")
    leads = clean_with_ai(raw)
    print(f"==> 过滤后剩余 {len(leads)} 条合格线索。")

    # 历史去重：仅推送未发送过的新线索
    history = load_sent_history()
    leads = dedupe_leads(leads, history)
    print(f"==> 去重后剩余 {len(leads)} 条新线索待推送。")

    # 网页邮箱提取：为每条线索抓取并解析其来源页面中的联系邮箱
    leads = enrich_leads_with_emails(leads)
    total_emails = sum(len(l.get("emails") or []) for l in leads)
    print(f"==> 邮箱提取完成：共从 {len(leads)} 条线索中解析到 {total_emails} 个邮箱。")

    html = build_html_report(leads, generated_at)

    # 始终保存产物，方便在 Actions 运行记录中查看
    with open("leads_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("leads_report.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "leads": leads},
                  f, ensure_ascii=False, indent=2)

    subject = f"每日潜在客户线索日报 · {now.strftime('%Y-%m-%d')} · {len(leads)} 条新线索"
    sent = send_email(subject, html)

    # 推送成功后才将本次线索写入历史缓存，确保不重复推送
    if sent:
        save_sent_history([l.get("source_url") for l in leads])
        print("==> 完成，历史缓存已更新。")
    else:
        print("==> 邮件发送未成功，本次线索不写入历史缓存（下次运行将重试）。",
              file=sys.stderr)


if __name__ == "__main__":
    main()
