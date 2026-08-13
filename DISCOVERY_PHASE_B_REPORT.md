# Discovery Lead Quality Hardening — Phase B 实施报告

> 只读审计见 `DISCOVERY_AUDIT.md`。本 Phase 仅解决 **Supplier / Competitor 漏网** 与
> **分类缺失** 问题，**未改动** lead_score / 阈值 / 搜索量 / freshness / cache。
> 按约束：**未 commit、未 push**。

---

## A. Files Changed

| 文件 | 修改目的 |
|------|----------|
| `lead_filter_engine.py` | B1：新增 `COMPETITOR_TITLE_RE`（通用「工艺+角色」组合语义识别）；修改 `is_competitor` 接入该规则（保留全部旧规则）；B3：新增 `classify_company()` / `_classify_text()` 7 类分类。 |
| `main.py` | B2：新增 `apply_post_ai_gates()`（AI 清洗后强制再跑 competitor + buyer 闸门）并接入 `main()`；新增 `_lead_text_blob()`；B4：新增 `_domain_of()` / `is_directory_listing_lead()` / `filter_directory_listings()` 并接入 `main()`；import 增加 `passes_buyer_gate`、`classify_company`；增加最小 funnel 计数打印。 |
| `tests/test_phase_b.py` | **新增** 14 项测试，覆盖 B1–B4（见 F）。 |
| `.workbuddy/skills/b2b-lead-filter-engine/assets/lead_filter_engine.py` | 同步更新为与根目录模块一致（项目级备份）。 |

---

## B. Competitor Detection（B1）

扩展 `is_competitor` / `filter_competitors`，新增 **组合语义** 规则 `COMPETITOR_TITLE_RE`：

- 形式：`工艺词(die casting / casting / machining / cnc / molding / metal …)` + `供应商角色词(supplier / manufacturer / services / provider / foundry / company / factory …)`。
- **刻意不做单词匹配**（如 `manufacturer` / `factory` 单独出现不判同行），只对「工艺 + 角色」组合命中，避免误杀买家页。
- 命中后加**买方意图豁免**：若同一文本命中 `TRUE_BUYER_RE`（与买方闸门一致，如 `We are looking for … supplier`、`request for quote`），则视为询盘而非供应商自广告 → 放行，交由买方闸门判定。
- **完整保留** 既有 `COMPETITOR_HARD_PHRASES`（`we are a manufacturer` / `our foundry` / `injection molding supplier` / `request a quote from us` …）与 `COMPETITOR_REGEX`。
- 覆盖用户列出的全部语义：`die casting supplier` / `alumin(i)um die casting supplier|services` / `casting manufacturer` / `metal casting supplier` / `injection molding supplier` / `CNC machining supplier|manufacturer` 等。

---

## C. Post-AI Gate（B2 — 最高优先级）

**核心原则：LLM 分类只是建议，确定性闸门仍是权威。**

修正后的最终 pipeline：

```
collect_raw_leads (含 site: 目录查询)
→ filter_blacklist
→ filter_competitors                 [is_competitor: 标题/摘要/URL]
→ filter_directory_listings (B4)    [目录站纯 Listing 剔除]
→ clean_with_ai                      [AI 清洗；失败/空 → 回退 clean_with_rules]
→ apply_post_ai_gates (B2)           ← 新增：AI 返回后强制再跑
     ├─ filter_competitors AGAIN      (is_competitor on need_summary + url，不含 keyword)
     ├─ passes_buyer_gate AGAIN       (TRUE_BUYER_RE 复查)
     └─ classify_company              (附加 7 类标签，仅辅助)
→ dedupe_leads → enrich → filter_competitor_emails
→ score_lead (未改) → build_html_report → send_email
```

- **AI 无法绕过 competitor gate**：即使 AI 标注 `BUYER`，只要 `need_summary` 命中同行特征，仍被剔除。
- **AI 无法绕过 buyer gate**：缺买方意图（即便 AI 标 BUYER）也被剔除。
- **关键修正**：competitor 闸门**只用线索实际内容**（`need_summary` + `url`），**不把搜索关键词 `keyword` 计入**——`keyword` 本质是买方意图查询（如 `seeking CNC machining supplier`），若计入会把真买家误判为同行。
- 实施中该修正修复了一个会被 `keyword` 触发的真实误杀（见 G）。

---

## D. 7-Class Classification（B3）

新增独立维度 `classify_company(lead)`，返回 7 类之一：
`BUYER / SUPPLIER / COMPETITOR / OEM / DISTRIBUTOR / SERVICE_PROVIDER / IRRELEVANT`。

- **与 `is_competitor` / `is_true_buyer` 解耦**：不删除、不替换既有布尔字段；仅在通过后闸门后的线索上写入 `_company_class` 辅助标签，不改变入库判定。
- 判定优先级（自上而下）：`COMPETITOR` → `DISTRIBUTOR` → `SERVICE_PROVIDER` → `SUPPLIER` → `OEM` → `BUYER` → `IRRELEVANT`。
- 约束落实：**不把 `manufacturer` 直接等同 `COMPETITOR`**（未触及同行硬规则时归 `SUPPLIER`）；**不把 `OEM` 等同 `BUYER`**（`OEM` 仅在同时表达采购意图 `is_true_buyer` 时才归 `OEM`）。

---

## E. Directory Filtering（B4）

针对 `site:` 定向查询命中的目录站（Thomasnet / Kompass / GlobalSpec / Engineering.com / reddit 等）：

- `is_directory_listing_lead(url, title, snippet)`：仅当 `域名 ∈ DIRECTORY_SITES` **且** 文本**缺乏买方意图**（`not passes_buyer_gate`）时，判定为纯 Supplier profile / Product listing → 剔除。
- **不对整个域名一刀切**：带 RFQ / 寻源 / 采购信号的目录页面（如 "Automotive OEM looking for … Submit RFQ"）正常放行，继续进入 buyer gate。
- 在 `main()` 中于 `filter_competitors` 之后、`clean_with_ai` 之前调用，提前削减目录噪声。

---

## F. Tests

| 项 | 数量 |
|----|------|
| 新增测试（`tests/test_phase_b.py`） | **14** |
| 原有测试 | 92 |
| **合计** | **106** |
| Passed | **106** |
| Failed | **0** |

新增测试清单（用户要求的 1–10 + 4 项加固）：
1. 通用供应商标题 `Aluminum Die Casting Services` → competitor
2. `Die Casting Supplier` → competitor
3. `Casting Manufacturer` → supplier/competitor（非 buyer）
4. 旧规则 `We are a manufacturer of aluminum die castings.` 仍识别
5. AI 误判 competitor（need_summary=`Aluminum Die Casting Supplier`）→ 后闸门 REJECT
6. AI 误判但缺买方意图 → 后闸门 REJECT（再跑 buyer gate）
7. 真实买方（含 supplier/manufacturer 词）→ 存活 + 标 `BUYER`
8. 目录 Listing 无买方意图 → REJECT
9. 目录 Listing 有买方意图（RFQ）→ 不域名误杀
10. 7 类分类各返回合法标签
11. 通用 `CNC Machining Manufacturer` / `Metal Casting Supplier` / `Injection Molding Supplier`
12. `is_competitor` 不误杀真实买方（`We are looking for …` / `Buyer seeking …`）
13. 后闸门对规则回退产物幂等（不误杀真买方）
14. 非目录域名不受目录规则影响

---

## G. Regression / Scope Audit

| 检查项 | 结果 |
|--------|------|
| `score_lead` 完全不变 | ✅ 函数体未改（仍 line 876；评分权重/email bonus/材质/参数/图纸/数量/高价值域名/tier≥70/≥45 全部原样） |
| threshold 完全不变 | ✅ `RULE_MIN_SCORE` / `TRUE_BUYER_RE` 闸门条件未改 |
| 搜索关键词不变 | ✅ `KEYWORD_GROUPS` 未动 |
| 搜索量 / `SEARCH_PER_KEYWORD` 不变 | ✅ 未改 |
| `freshness` 不变 | ✅ Bing `freshness` 未动 |
| `sent_cache.json` 去重 / `HISTORY_MAX` 不变 | ✅ 未改 |
| `lead_score` / `sales_priority` / `Opportunity` / `Intent Engine` | ✅ 本仓库不存在这些组件，未创建/模拟 |
| `git diff --check` | ✅ 无空白/行尾错误（exit 0） |

**实施中捕获并修复的 2 个瞬态回归（均已解决，无遗留失败）：**
- (a) `is_competitor` 初版用「第一人称」豁免过窄，误杀 `Buyer seeking CNC machining supplier … request for quote` → 改为与买方闸门一致的 `TRUE_BUYER_RE` 豁免，真实买方不再被误杀。
- (b) 后闸门初版把搜索 `keyword` 计入 competitor 判定，会误杀真买家（keyword 本身是买方查询含 `supplier`）→ 修正为 competitor 闸门只用 `need_summary + url`。

**原有 regression 测试**：本 Phase 前基线 92 passed；本 Phase 后 106 passed。**无由本 Phase 引起的新失败**（上述 2 个均为实施期自测发现并已修复）。

---

## H. Git Status

```
$ git status --short
 M .workbuddy/skills/b2b-lead-filter-engine/assets/lead_filter_engine.py
 M lead_filter_engine.py
 M main.py
?? DISCOVERY_AUDIT.md
?? tests/test_phase_b.py

$ git diff --stat
 .../assets/lead_filter_engine.py                 | 103 ++++++
 lead_filter_engine.py                            | 103 ++++++
 main.py                                          | 112 ++++++-   (仅 import 行 -1 重构 + 新增函数)
 3 files changed, 317 insertions(+), 1 deletion(-)

$ git diff --check
(无输出，exit 0 → 干净)
```

- **未 commit**（按约束）。
- **未 push**（按约束）。
- 无无关文件修改：仅 3 个目标文件 + 1 个新增测试文件；`DISCOVERY_AUDIT.md` 为上阶段产物（仍 untracked）。

---

## 验收标准核对

1. ✅ 通用 Supplier/Competitor 标题可识别（`COMPETITOR_TITLE_RE`）
2. ✅ AI clean 后一定重新执行 competitor gate（`apply_post_ai_gates`）
3. ✅ AI clean 后一定重新执行 buyer gate
4. ✅ AI 无法绕过确定性 gate
5. ✅ Supplier 目录 Listing 无买方意图时被过滤（`filter_directory_listings`）
6. ✅ 有买方意图的目录页面不被域名规则误杀
7. ✅ 七类 classification 正常工作
8. ✅ `lead_score` 完全不变
9. ✅ threshold 完全不变
10. ✅ Search volume / freshness / cache 完全不变
11. ✅ 新增测试全部通过（14/14）
12. ✅ 原有 regression 测试无本 Phase 引入的新失败
13. ✅ `git diff --check` 通过
14. ✅ 未 commit
15. ✅ 未 push
