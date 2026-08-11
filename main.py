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
  2. 调用大模型 API 清洗、过滤垃圾信息，抽取结构化线索
  3. 生成美观的 HTML 日报，通过 SMTP (SSL / STARTTLS) 发送至指定邮箱

所有敏感配置均来自环境变量 / GitHub Secrets，不写死在代码中。
"""

import os
import json
import sys
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

# 目标行业关键词，可自行增减
KEYWORDS = [
    "CNC machining RFQ",
    "aluminum die casting inquiry",
    "plastic mold RFQ",
    "custom casting buyer",
    "plastic injection molding parts sourcing",
    "die casting buyer request for quote",
]

SEARCH_PER_KEYWORD = int(os.getenv("SEARCH_PER_KEYWORD", "10"))
RESULTS_LIMIT = int(os.getenv("LEADS_LIMIT", "15"))


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
    results = []
    for kw in KEYWORDS:
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


def clean_with_ai(raw_results):
    api_key = cfg("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        print("[ai] OPENAI_API_KEY 缺失或 openai 未安装；跳过 AI 过滤，"
              "回退为原始结果直通。", file=sys.stderr)
        return [{
            "company": "Unknown",
            "need_summary": (r.get("snippet") or "")[:200],
            "source_url": r.get("url", ""),
            "keyword": r.get("keyword", ""),
            "confidence": "low",
        } for r in raw_results[:RESULTS_LIMIT]]

    client = OpenAI(
        api_key=api_key,
        base_url=cfg("OPENAI_BASE_URL") or None,   # 兼容 Azure / OpenRouter 等
    )
    model = cfg("OPENAI_MODEL", "gpt-4o-mini")

    payload = json.dumps(raw_results, ensure_ascii=False, indent=2)
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
    except Exception as e:
        print(f"[ai] cleaning failed: {e}", file=sys.stderr)
        return []
    return leads[:RESULTS_LIMIT]


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_html_report(leads, generated_at):
    if not leads:
        body = ('<p class="empty">今天没有匹配到高质量的潜在客户线索。'
                '可能是搜索结果较少，或 AI 过滤未通过。明天继续监控。</p>')
    else:
        rows = []
        for i, l in enumerate(leads, 1):
            conf = (l.get("confidence") or "low").lower()
            badge = {
                "high": '<span class="badge high">高</span>',
                "medium": '<span class="badge medium">中</span>',
                "low": '<span class="badge low">低</span>',
            }.get(conf, '<span class="badge low">低</span>')
            url = l.get("source_url", "") or "#"
            rows.append(f"""
            <tr>
              <td class="num">{i}</td>
              <td><strong>{esc(l.get('company', 'Unknown'))}</strong></td>
              <td>{esc(l.get('need_summary', ''))}</td>
              <td>{esc(l.get('keyword', ''))}</td>
              <td>{badge}</td>
              <td><a href="{esc(url)}" target="_blank" rel="noopener">查看来源</a></td>
            </tr>""")
        body = f"""
        <table>
          <thead>
            <tr>
              <th>#</th><th>客户 / 买家</th><th>需求摘要</th>
              <th>触发关键词</th><th>置信度</th><th>来源</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日潜在客户线索日报</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; background:#f4f6f9; margin:0; color:#1f2d3d; }}
  .wrap {{ max-width: 920px; margin: 0 auto; padding: 28px 20px; }}
  .header {{ background: linear-gradient(135deg,#0d4a8e,#1e88e5); color:#fff; border-radius:12px; padding:24px 28px; }}
  .header h1 {{ margin:0 0 6px; font-size:22px; }}
  .header p {{ margin:0; opacity:.9; font-size:13px; }}
  .card {{ background:#fff; border-radius:12px; padding:22px; margin-top:18px; box-shadow:0 2px 10px rgba(0,0,0,.05); overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th, td {{ text-align:left; padding:11px 10px; border-bottom:1px solid #eef1f5; vertical-align:top; }}
  th {{ background:#f7f9fc; color:#5a6b7b; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
  .num {{ color:#9aa7b4; width:28px; }}
  a {{ color:#1e88e5; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .badge {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; }}
  .badge.high {{ background:#e6f7ec; color:#1a8a4f; }}
  .badge.medium {{ background:#fff4e0; color:#b9770e; }}
  .badge.low {{ background:#f0f2f5; color:#7a8794; }}
  .empty {{ color:#7a8794; padding:14px 0; }}
  .footer {{ text-align:center; color:#9aa7b4; font-size:12px; margin-top:20px; }}
</style></head>
<body><div class="wrap">
  <div class="header">
    <h1>🔧 每日潜在客户线索日报</h1>
    <p>垂直行业：CNC 加工 · 压铸 (Die Casting) · 铸造 (Casting) · 塑胶模具与注塑</p>
    <p>生成时间：{generated_at}</p>
  </div>
  <div class="card">{body}</div>
  <div class="footer">本邮件由 GitHub Actions 自动生成 · 共 {len(leads)} 条线索</div>
</div></body></html>"""


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

    html = build_html_report(leads, generated_at)

    # 始终保存产物，方便在 Actions 运行记录中查看
    with open("leads_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("leads_report.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "leads": leads},
                  f, ensure_ascii=False, indent=2)

    subject = f"每日潜在客户线索日报 · {now.strftime('%Y-%m-%d')} · {len(leads)} 条"
    send_email(subject, html)
    print("==> 完成。")


if __name__ == "__main__":
    main()
