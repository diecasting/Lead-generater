# Discovery Phase A2.1 — Configurable Search Freshness (Report)

> **Phase 状态**：已实施 / 已测试 / 已审计。**NOT committed / NOT pushed**。
> 基于 `DISCOVERY_PHASE_A2_1_AUDIT.md`（Verdict: READY FOR IMPLEMENTATION）。

---

## A. Files Changed

| 文件 | 改动 |
|------|------|
| `main.py` | 顶部新增 `DISCOVERY_SEARCH_FRESHNESS` 配置常量（含 `_ALLOWED_FRESHNESS` 与非法值回退）；`bing_search()` 的 `params["freshness"]` 改为引用该常量 |
| `README.md` | 配置表新增 `DISCOVERY_SEARCH_FRESHNESS` 一行说明 |
| `tests/test_phase_a2_1.py` | **新增** 9 个 freshness 测试（默认 / Day / Month / 非法值 / 实际请求参数 / import-time reload 行为） |
| `DISCOVERY_PHASE_A2_1_AUDIT.md` | 本 Phase 的设计审计文档（上一阶段产物，保留） |

> `DIFFSTAT`：`main.py | 15 ++++++++++++++-, README.md | 1 +`；新增测试 + 审计共 2 个未跟踪文件。

---

## B. Configuration

```text
DISCOVERY_SEARCH_FRESHNESS = os.getenv("DISCOVERY_SEARCH_FRESHNESS", "Week")

_ALLOWED_FRESHNESS = ("Day", "Week", "Month")
```

- **默认**：`Week`（与改动前硬编码行为完全一致，零回归）。
- **允许值**：`Day` / `Week` / `Month`（Bing Web Search v7 规范值，规范大小写，不做额外 normalization）。
- 复用项目既有 `os.getenv("NAME", "default")` 模块级常量模式，未新建 config 框架。

---

## C. Validation

```text
configured value
      │
      ├─ in ("Day","Week","Month") ──► 使用原值
      │
      └─ 不在允许集合 ──► 打印 [config][WARN] 警告 ──► 回退 "Week"
```

- 非法值（如 `InvalidValue`、空串、`year`）**不会 crash**，discovery 正常运行并使用 `Week`。
- 警告输出到 `stderr`，便于 CI 日志排查。
- 不引入新的异常或退出码。

---

## D. Bing Request

`bing_search()` 内改动（main.py）：

```python
params = {
    "q": query,
    "count": count,
    "mkt": "en-US",
    "freshness": DISCOVERY_SEARCH_FRESHNESS,   # 原硬编码 "Week" 改为配置常量
    "textDecorations": False,
}
```

最终发送给 `https://api.bing.microsoft.com/v7.0/search` 的 `params["freshness"]` 等于配置值（默认 `Week`）。测试已用 mock `requests.get` 断言真实 request params。

---

## E. DDG

- **DDG 回退实现未改动**（`ddg_search()` 原样保留）。
- 当前架构：`BING_API_KEY` 存在 → Bing（freshness 生效）；缺失 → DDG（无 freshness 概念）。
- 文档（README + 本报告）明确声明：
  > `DISCOVERY_SEARCH_FRESHNESS` applies to the Bing search path only. The DDG fallback path does not expose an equivalent freshness parameter.
- 未尝试在 DDG 上伪造 freshness。

---

## F. Tests

| 项 | 数量 |
|----|------|
| 实施前（Phase B + A1） | 122 passed |
| 新增 A2.1 测试 | 9 |
| **总计** | **131 passed** |
| failed | **0** |
| unexpected regression | **0** |

新增测试覆盖：
1. 默认常量显式为 `Week`（防未来静默改默认值）。
2. `Day` 合法值透传（import-time reload 验证真实运行行为）。
3. `Month` 合法值透传（import-time reload 验证）。
4. 非法值 `InvalidValue` → 警告 + 回退 `Week`（reload 验证，不 crash）。
5. 实际 Bing request params 携带配置值（mock `requests.get`，验证 `Day`）。
6. 默认 request params 为 `Week`（mock，验证不改默认行为）。
7. import-time：设置 env `Month` 后 reload → 模块常量 = `Month`。
8. import-time：非法 env `Bogus` → 警告 + `Week`（capsys 验证 WARN 输出）。
9. 空串 env → 回退 `Week`。

测试隔离：通过 `monkeypatch.setenv` + `importlib.reload(main)` + `finally` 还原，避免模块级常量 import-time 求值导致的跨测试污染；请求类测试直接 `monkeypatch.setattr(main, "DISCOVERY_SEARCH_FRESHNESS", ...)` 后 mock，无假测试。

---

## G. Scope Audit

| Forbidden 项 | 状态 |
|--------------|------|
| `SEARCH_PER_KEYWORD` | ✅ 未改（仅 `count=SEARCH_PER_KEYWORD` 默认参数保留） |
| `RESULTS_LIMIT` | ✅ 未改 |
| pagination | ✅ 未改（无分页逻辑） |
| deep crawl | ✅ 未改 |
| dedup / `DISCOVERY_DEDUP_TTL_DAYS` / `HISTORY_MAX` | ✅ 未改 |
| `lead_score` / 阈值 | ✅ 未改 |
| buyer gate / `passes_buyer_gate` / `TRUE_BUYER_RE` | ✅ 未改 |
| competitor gate / `COMPETITOR_TITLE_RE` / `filter_competitors` | ✅ 未改 |
| `classify_company` / `RFQ_PLATFORM_RE` | ✅ 未改 |
| AI cleaning | ✅ 未改 |
| DDG search | ✅ 未改 |
| keyword groups | ✅ 未改 |
| 搜索量 / 关键词数量 | ✅ 未增加 |

`git diff --check` 干净；`git diff --stat` 仅 `main.py (+14/-1)` 与 `README.md (+1)`。

---

## H. Git Status

```text
HEAD = 59f5de3  feat(discovery): add TTL-based sent history   (Phase A1)
Phase B = 4eeea58

Working tree:
   M README.md
   M main.py
   ?? DISCOVERY_PHASE_A2_1_AUDIT.md
   ?? tests/test_phase_a2_1.py

A2.1 = implemented / tested (131 passed) / audited
A2.1 = NOT committed
A2.1 = NOT pushed
A2.2 = NOT started
```

---

## Final Stop Condition

```text
Phase B  → 4eeea58
Phase A1 → 59f5de3
Phase A2.1 → implemented / tested / audited
Working tree → A2.1 changes only

A2.1 → NOT committed
A2.1 → NOT pushed
A2.2 → NOT started
```

未修改搜索量 / 关键词 / 分页 / 深抓 / scoring / 阈值 / buyer gate / competitor gate / dedup / DDG。
