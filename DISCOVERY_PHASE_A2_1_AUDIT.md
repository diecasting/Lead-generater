# Discovery Phase A2.1 — Freshness Configuration Audit & Design

> **本阶段性质：Read-only Audit + Design。**
> 未修改任何代码 / 测试 / 配置 / GitHub Actions / cache / README。
> 未 commit、未 push、未进入 A2.2。

---

## A. Current Git State

```text
HEAD        = 59f5de3  feat(discovery): add TTL-based sent history   (Phase A1)
HEAD~1      = 4eeea58  fix(discovery): harden lead precision gates   (Phase B)
Working tree = clean   (git status --short 为空，git diff --check 干净)
Tests       = 122 passed / 0 failed (前序已确认，本阶段未跑以严格保持只读；
              如需复跑请用 pytest -q，不应产生变化)
```

基线符合预期，可安全进入设计。

---

## B. Current Freshness Implementation

唯一实际代码位置：`main.py:281`，在 `bing_search()` 内部硬编码：

```python
def bing_search(query, api_key, count=SEARCH_PER_KEYWORD):
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
    ...
```

要点：
- `freshness` 是 **字符串字面量 `"Week"`**，作为 Bing Web Search v7 的 query 参数。
- **无类型转换、无校验、无 enum**；直接拼进 `params`。
- 不是 `os.getenv`、不是函数默认参数、不是常量名引用。
- 全局仅此一处定义，亦仅此一处传入（无其他分支覆盖不同 freshness）。

---

## C. Actual Search Call Chain

```text
main()
  └─ collect_raw_leads()                     main.py:363
       ├─ bing_key = cfg("BING_API_KEY")      main.py:364
       ├─ keywords = get_search_keywords()    main.py:365
       ├─ loop kw in keywords:
       │    ├─ if bing_key: bing_search(kw, bing_key)   main.py:374
       │    └─ else:        ddg_search(kw)              main.py:376
       └─ loop q in get_directory_queries(keywords):
            ├─ if bing_key: bing_search(q, bing_key)    main.py:388
            └─ else:        ddg_search(q)               main.py:390
  └─ raw = collect_raw_leads()                main.py:1419
  └─ (后续：dedup → competitor/buyer gates → AI → post-AI gates → qualified → send)
```

`freshness` 的流向：
1. **定义**：`bing_search()` 内 `params["freshness"] = "Week"`（main.py:281）。
2. **传入**：由 Bing endpoint 请求 `params` 携带（main.py:284）。
3. **转换**：无转换。
4. **进入的 API 参数**：Bing Web Search v7 的 `freshness` query 参数。
5. **是否所有 Discovery query 使用相同 freshness**：是。普通关键词搜索与定向 `site:` 搜索都走同一个 `bing_search()`，共享同一 `freshness`。
6. **是否存在不同 freshness 的分支**：否。
7. **fallback/default**：硬编码 `"Week"` 即默认；无运行期 fallback 逻辑。

**重要运行期事实**：`bing_search()` 仅在 `BING_API_KEY` 存在时被调用；否则走 `ddg_search()`（main.py:311），而 `ddg_search` **完全不使用也不支持 freshness 参数**（DuckDuckGo HTML 端点无该参数）。因此：
> 当前 `freshness="Week"` 仅在配置了 `BING_API_KEY` 时真正生效。DDG 回退路径不受 freshness 影响。

---

## D. Search Provider Architecture

- **Bing Web Search API v7**（`https://api.bing.microsoft.com/v7.0/search`，Ocp-Apim-Subscription-Key 鉴权）——`bing_search()`。
- **DuckDuckGo HTML 回退**（`https://html.duckduckgo.com/html/`，无 key）——`ddg_search()`。
- 二者为**函数级平铺实现**，无 Provider 抽象类 / interface。调用方 `collect_raw_leads()` 按 `bing_key` 有无二选一。
- `freshness` 是 **provider-specific**（Bing 专有概念）；DDG 无对应参数。
- 结论：未来配置化应把值放在 **discovery 配置层**（模块级 `os.getenv`），由 `bing_search()` 读取后传入 Bing `params`；**不应**为 DDG 伪造 freshness 参数。配置消费点明确在 Bing 实现内。

---

## E. Existing Configuration Pattern

项目已建立一致的模块级 env 读取模式（`main.py` 顶部常量区）：

```python
SEARCH_PER_KEYWORD      = int(os.getenv("SEARCH_PER_KEYWORD", "10"))      # 164
RESULTS_LIMIT           = int(os.getenv("LEADS_LIMIT", "20"))            # 165
HISTORY_MAX             = int(os.getenv("HISTORY_MAX", "2000"))          # 173
DISCOVERY_DEDUP_TTL_DAYS= int(os.getenv("DISCOVERY_DEDUP_TTL_DAYS", "30"))# 178
DIRECTORY_SEARCH        = str(os.getenv("DIRECTORY_SEARCH", "1"))...     # 190
DIRECTORY_MAX_QUERIES   = int(os.getenv("DIRECTORY_MAX_QUERIES", "12"))  # 191
```

规律：
- 命名 `UPPER_SNAKE`；默认值作为 `os.getenv` 第二参。
- 数值型用 `int(...)` 强转（注意：非法值会在 import 期 `ValueError` 崩溃）。
- 布尔型用 `str(...).lower() in ("1","true","yes")`。
- 字符串型直接 `os.getenv("X", "default")`。

**推荐 A2.1 复用此模式**：新增一个字符串型常量即可，无需新建 config framework / yaml / config.py。

---

## F. `.env` / Environment Variables

```text
find . -maxdepth 2 ( -name ".env.example" -o -name "*.env.example" -o -name ".env" )  → 无结果
```

- **不存在** `.env.example` 或 `.env` 文件。
- 现有 discovery 相关变量（如 `SEARCH_PER_KEYWORD`、`RESULTS_LIMIT`、`HISTORY_MAX`、`DISCOVERY_DEDUP_TTL_DAYS`、`BING_API_KEY`）**仅由代码读取 + README 文档**，没有 example 文件。
- 因此 A2.1 应：
  - 在 `main.py` 顶部加 `DISCOVERY_SEARCH_FRESHNESS = ...`（符合现有模式）；
  - 在 `README.md` 配置表补一行说明（属于实现阶段文档改动，本审计不改）；
  - **不**创建新的 `.env.example`（与现有项目习惯一致，避免引入新框架）。

---

## G. GitHub Actions Environment Injection

`.github/workflows/daily_leads.yml`（只读，未改）：

- Discovery job `collect-leads` 在 **job 级 `env:`** 块注入 Secrets：
  ```yaml
  env:
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    BING_API_KEY:   ${{ secrets.BING_API_KEY }}
    ...
  ```
- 变量来源：全部为 **GitHub Secrets**（加密），非 repo Variables。
- `python main.py` 直接继承 job `env`（main.py:48）。
- **freshness 不在 workflow 中**；如需在 CI 启用配置，最合理注入位置是在 job 级 `env:` 中加：
  ```yaml
  DISCOVERY_SEARCH_FRESHNESS: ${{ secrets.DISCOVERY_SEARCH_FRESHNESS }}
  ```
  （或改用 `vars.DISCOVERY_SEARCH_FRESHNESS` 若希望非敏感可调）。
- 实现阶段再决定是否编辑 workflow；**本审计不修改 workflow**。

---

## H. Supported Freshness Values

依据 Bing Web Search API v7 规范与当前实现：

```text
Currently used (本项目):
  - Week        (main.py:281 硬编码)

Supported by Bing API (provider spec):
  - Day
  - Week
  - Month
```

- 其他 Bing `freshness` 形态（如 `2019-01-01..2019-12-31` 日期区间）本设计**不纳入**，保持最小安全集合 `{Day, Week, Month}`。
- `ddg_search` 无 freshness 概念，配置对其无效果（provider-specific 限制）。

---

## I. Existing Test Coverage

```text
grep -Rn "freshness" tests → 仅 tests/test_phase_b.py:8 的注释提及（"不改动 freshness"），无断言。
```

- **无** search request 断言测试（未 mock `requests.get` 来校验 `params`）。
- **无** provider 单测 / integration 测试针对 freshness。
- **无** config 测试针对 freshness 默认值。
- **无** invalid value 测试。
- 现有 search 相关测试仅有 `tests/test_email_extract.py` 中的 `get_search_keywords` 测试（与 freshness 无关）。

结论：A2.1 需**新增**针对 freshness 的测试（mock `requests.get`，断言 `params["freshness"]`）。这是净增覆盖，不影响既有 122 测试。

---

## J. Hidden Coupling / Risks

逐项核对 `freshness` 与以下逻辑的耦合：

| 逻辑 | 是否耦合 | 说明 |
|------|----------|------|
| dedup (A1 TTL) | 否 | freshness 只改变搜索结果池，不进 sent_cache key/value |
| competitor filter | 否 | 作用于清洗后 lead，与搜索时间窗无关 |
| buyer gate | 否 | 同上 |
| AI cleaning | 否 | 同上 |
| score_lead | 否 | 同上 |
| SEARCH_PER_KEYWORD | 否 | 仅 `count` 参数，freshness 独立 |
| RESULTS_LIMIT | 否 | 仅最终结果条数上限 |
| pagination | 否 | 无分页逻辑 |

**结论**：`freshness` 仅影响 `search result pool`，不耦合任何确定性闸门。放宽到 `Month` 时，A1 的 TTL dedup 仍会拦截 30 天内已发送线索——二者协同正常，不会出现"旧 lead 被重复发送"。

**已知风险（低，可在实现期规避）**：
- `ddg_search` 不支持 freshness：配置对无 `BING_API_KEY` 的运行完全无效，需在文档/日志中明确。
- 默认 `"Week"` 全局唯一，配置后必须保证所有 `bing_search` 调用读同一常量（避免某处遗漏硬编码）。
- 非法值：当前模式 `int(...)` 会崩溃，但 freshness 是字符串，若不校验会被原样发给 Bing（Bing 对非法 freshness 返回错误 → 整次搜索失败）。需设计 fallback。

---

## K. Minimal Configuration Design

最小、安全、复用现有模式的方案（**现在不实现**）：

1. **配置常量**（main.py 顶部常量区，贴近 `DISCOVERY_DEDUP_TTL_DAYS`）：
   ```python
   # Bing 搜索时间窗（仅影响启用了 BING_API_KEY 的 Bing 路径；DDG 回退忽略）
   _ALLOWED_FRESHNESS = ("Day", "Week", "Month")
   DISCOVERY_SEARCH_FRESHNESS = os.getenv("DISCOVERY_SEARCH_FRESHNESS", "Week")
   if DISCOVERY_SEARCH_FRESHNESS not in _ALLOWED_FRESHNESS:
       print(f"[config][WARN] DISCOVERY_SEARCH_FRESHNESS="
             f"{DISCOVERY_SEARCH_FRESHNESS!r} 非法，回退为 'Week'",
             file=sys.stderr)
       DISCOVERY_SEARCH_FRESHNESS = "Week"
   ```
2. **消费点**（main.py:281）：
   ```python
   "freshness": DISCOVERY_SEARCH_FRESHNESS,
   ```
3. **不新增** config 文件、不新建 abstraction、不动 `bing_search` 签名（保持 `count=SEARCH_PER_KEYWORD` 默认不变）。

设计刻意与 `int(...)` 强转模式不同：字符串 freshness 采用 **warn+fallback** 而非崩溃，因为非法值导致的搜索失败会比"使用 Week"更严重，符合 A1 审计中"invalid value safety"精神。也避免了 `int()` 在 import 期崩溃的行为不一致。

---

## L. Proposed Environment Variable

```text
DISCOVERY_SEARCH_FRESHNESS
```

- 类型：字符串（枚举 Day | Week | Month）
- 默认：`Week`（与当前行为完全一致，满足 Requirement 1）
- 注入位置：进程环境变量（本地 / CI job env）
- 作用域：仅 Bing 搜索路径

---

## M. Default / Invalid Value Behavior

| 场景 | 行为 |
|------|------|
| 未设置 env | `DISCOVERY_SEARCH_FRESHNESS = "Week"` → 与现状一致（Req 1 ✓） |
| `Day` / `Week` / `Month` | 原样传入 Bing `params["freshness"]`（Req 2 ✓） |
| 非法值（如 `Bogus`、`""`、`year`） | 打印 `[config][WARN] ... 回退为 'Week'`，使用 `Week`，discovery 不崩溃（Req 3 ✓） |

> 注：若后续希望"非法值即硬失败"可与现有 `int()` 风格对齐，但本设计优先不崩溃。最终以用户实现指令为准。

---

## N. Proposed Tests (新增 `tests/test_phase_a2_1.py`)

需 mock `requests.get` 以拦截 `params`。

1. **Test — Default**：不设置 env → `bing_search` 发出的请求 `params["freshness"] == "Week"`。
2. **Test — Custom Valid (Day)**：`monkeypatch.setenv("DISCOVERY_SEARCH_FRESHNESS","Day")` → 请求 `params["freshness"] == "Day"`。
3. **Test — Custom Valid (Month)**：设为 `Month` → 请求 `params["freshness"] == "Month"`。
4. **Test — Invalid Value**：设为 `Bogus` → 不抛异常，最终请求 `params["freshness"] == "Week"`（fallback），且 stderr 含 WARN。
5. **Test — Request Assertion**：mock `requests.get`，断言 Bing endpoint 实际收到配置后的 freshness（防止值被静态覆盖漏改）。
6. **Test — Config Constant Default**：直接断言模块级 `main.DISCOVERY_SEARCH_FRESHNESS` 默认 `"Week"`，且非法值在 import 路径外被规整（可用 helper 封装校验逻辑以便单测，避免 import 期副作用）。

> 这些测试均为净增，不修改既有 122 测试；实施后应得 122 + N passed。

---

## O. Expected Impact

仅基于代码逻辑的理论影响（未执行真实大量搜索）：

| freshness | Bing 时间窗 | 对 result pool 的理论影响 |
|-----------|-------------|---------------------------|
| `Day` | 最近 24h | 候选池最小、最新；可能漏掉稍旧但有效的 RFQ |
| `Week` | 最近 7 天 | 当前默认；平衡 |
| `Month` | 最近 30 天 | 候选池最大、更多旧 RFQ 重新进入；可能引入更多噪声 |

**关键说明**：
- `freshness` 配置化**本身不会自动增加 Lead 数量**——它只是把时间窗变为可实验变量。
- 配合 A1 的 TTL dedup（默认 30 天）：放宽到 `Month` 时，过去 30 天内已发送线索仍被 dedup 拦截，故不会"重复发送"，但会让**未发送过的旧 RFQ** 重新进入候选池，从而可能提升每日净新 lead。
- 这是 controlled experiment 能力，不是数量增益保证。

---

## P. Recommended Implementation Files

| 文件 | 改动 |
|------|------|
| `main.py` | 顶部加 `DISCOVERY_SEARCH_FRESHNESS` 常量（+合法性校验）；main.py:281 改为引用该常量 |
| `tests/test_phase_a2_1.py` | **新增** 6 个 freshness 测试（mock `requests.get`） |
| `README.md` | 配置表补 `DISCOVERY_SEARCH_FRESHNESS`（默认 `Week`，可选 Day/Week/Month，仅 Bing 路径）一行 |
| `.github/workflows/daily_leads.yml` | **可选**：job env 增加 `DISCOVERY_SEARCH_FRESHNESS: ${{ secrets.DISCOVERY_SEARCH_FRESHNESS }}`（若需在 CI 启用） |

**禁止改动**（Scope Guard）：`score_lead`、阈值、`SEARCH_PER_KEYWORD`、`RESULTS_LIMIT`、分页、深抓、buyer gate、competitor 检测、AI 分类、`lead_filter_engine.py`、`.workbuddy/` skill 副本。

---

## Q. Final Verdict

```text
READY FOR IMPLEMENTATION
```

理由：
- 改动面极小且局部（单函数常量 + 单参数引用）。
- 复用既有 `os.getenv` 配置模式，无新框架。
- 默认行为严格保持 `Week`，零行为回归风险。
- freshness 不与任何确定性闸门耦合，放宽不影响 qual/score/filter。
- 非法值有 warn+fallback 兜底，不会崩溃。
- 测试可净增覆盖，无既有测试被破坏。
- 唯一已知限制（DDG 路径忽略 freshness、需在文档说明）为低风险提示，不构成阻塞。

---

## Final Stop Condition

```text
HEAD                     = 59f5de3 (Phase A1)
Working tree             = clean (+ 本审计报告 DISCOVERY_PHASE_A2_1_AUDIT.md 未跟踪)
A2.1 implementation      = NOT started
A2.2 / pagination        = NOT started
No commit
No push
```

本阶段仅执行 Read-only Audit + Design，未对代码/测试/配置/workflow 做任何修改。
