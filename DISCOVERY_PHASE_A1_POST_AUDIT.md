# Discovery Phase A1 — Post-Implementation Audit (READ ONLY)

> 本审计为**只读**：未修改任何代码 / 测试 / 配置 / cache，未 commit，未 push，未进入 A2。
> 所有结论均以来 `main.py`、`.github/workflows/daily_leads.yml`、`tests/` 的**实际代码**为准。
>
> 审计基准：`HEAD = 4eeea58 fix(discovery): harden lead precision gates`（Phase B commit）
> A1 working tree：未提交、未推送；全量测试 **122 passed / 0 failed**。

---

## A. Git State

```text
$ git status --short
 M README.md
 M main.py
 M tests/test_resilience.py
?? DISCOVERY_PHASE_A1_REPORT.md
?? tests/test_phase_a1.py

$ git diff --check        # (空) 无 whitespace 错误
$ git log -1 --oneline
4eeea58 fix(discovery): harden lead precision gates
```

- `HEAD` = `4eeea58`（Phase B commit），确认。
- Working tree 仅含 A1 相关改动：
  - `main.py`（A1 dedup 重写 + `DISCOVERY_DEDUP_TTL_DAYS` 配置）
  - `tests/test_resilience.py`（2 个既有测试随新 dict 返回类型最小化适配）
  - `README.md`（文档：新增 TTL 配置与缓存格式说明）
  - `tests/test_phase_a1.py`（新增 16 测试，未跟踪）
  - `DISCOVERY_PHASE_A1_REPORT.md`（报告，未跟踪）
- **未发现任何非 A1 修改**。`git diff --check` 干净。

---

## B. Actual Cache Call Flow（实际调用链）

全部以 `main.py` 实际代码为准（行号对应 A1 后状态）：

```text
main()  line 1441: history = load_sent_history()
   └─ load_sent_history()  (line 1139)
        ├─ 读 sent_cache.json
        ├─ 新版 {"entries":...}  → 解析 entries，容错非法 entry  (1153-1164)
        ├─ 旧版 list / {"urls":[...]} → _migrate_legacy()  (1166)
        └─ 返回前 prune_expired_entries()  (1164 / 1166)  → 内存中仅存活记录

main()  line 1442: leads = dedupe_leads(leads, history)
   └─ dedupe_leads()  (line 1202)
        ├─ 对 history(dict) 再次防御性 prune_expired_entries()  (1211)
        └─ key in live_keys ? drop : keep

… lead processing（enrich / score / send_email）…

main()  line 1471: sent = send_email(...)
main()  line 1474-1475: if sent: save_sent_history([urls])
   └─ save_sent_history()  (line 1169)
        ├─ load_sent_history()  → 已裁剪存活记录  (1182)
        ├─ upsert：已存在键刷新 last_seen，新键 first_seen=last_seen=now  (1183-1187)
        ├─ 防御性 prune_expired_entries()  (1189)
        ├─ HISTORY_MAX 裁剪（按 last_seen 最旧优先）  (1191-1193)
        └─ json.dump 写入 sent_cache.json  (1194-1197)

GitHub Actions:
   daily_leads.yml: actions/cache@v4 (key sent-history-cache-v1, path sent_cache.json)
   → 下次 run 的 load_sent_history() 恢复同一文件
```

**关键点确认：**
- cache **读取**：`load_sent_history()`，在 dedup 之前（line 1441）。
- cache **prune**：`load_sent_history` 内（加载即裁剪）+ `dedupe_leads` 与 `save_sent_history` 内防御性再裁剪。
- dedup **判断**：`dedupe_leads`，line 1442（在 Phase B gates 之后）。
- cache **更新**：`save_sent_history` 内 upsert（line 1183-1187）。
- cache **保存**：`save_sent_history` 写盘（line 1194-1197）。
- `sent_cache` **在"最终 qualified / sent"时才更新**（line 1474 `if sent:`），**不是**在"发现 Lead"时。

---

## C. TTL Semantics

实际判定（`_entry_is_expired`, line 1089-1098）：

```python
last_seen = float(entry.get("last_seen", 0))
return (now - last_seen) >= ttl_days * 86400
```

- TTL 基准 = **`last_seen`**（而非 `first_seen`）。
- 边界：`age >= TTL` ⇒ 过期 ⇒ 允许重新进入。

```text
TTL type: SLIDING TTL
```

理由：`last_seen` 会在每次成功发送（资深合格线索）时向前滑动（见 §D）。这完全符合 A1 设计目标——"30 天生命周期去重"：一条线索在最近 30 天内被成功发送过则压制，超过 30 天（从上次发送算）可重新进入。Sliding TTL 对"避免重复发信"的场景是恰当且符合预期的。

---

## D. last_seen Refresh Timing（审计重点）

`save_sent_history()` 的**唯一调用点**是 `main.py:1475`，且位于：

```python
sent = send_email(subject, html)
if sent:
    save_sent_history([l.get("source_url") for l in leads])
```

因此 `last_seen` **仅在"合格线索被成功邮件发送"时刷新**。

逐状态核查：

| 业务状态 | 是否刷新 last_seen | 说明 |
|---|---|---|
| 被 competitor gate 拒绝 | ❌ 否 | 该线索未进入 `leads`（line 1436 之后），不会到达 save |
| 被 buyer gate 拒绝 | ❌ 否 | 同上 |
| 被 dedup 拒绝（历史已发） | ❌ 否 | 已命中缓存，未重新 `sent`，`last_seen` 保持原始发送时间 |
| qualified + 发送成功 | ✅ 是 | `if sent:` 分支内 upsert `last_seen = now` |
| 仅作为 search result（未合格/未发送） | ❌ 否 | **从不**刷新 |

**审计结论（重点）：**
- 原始 search result / 被任意 gate 拒绝的线索**永远不会**刷新 `last_seen`。
- 因此**不存在**"每次搜索都刷新 → 永久 not expired → 无限 sliding suppression"的风险。
- 唯一会滑动的是"持续被成功发送的合格线索"——这恰恰是预期的（避免每天重复发同一 URL）。该线索一旦停止出现超过 30 天，`last_seen` 不再滑动，`age >= TTL` 后自然过期、可重新进入。

✅ 本审计最关注的无限 suppression 风险**不存在**。

---

## E. Expiration / HISTORY_MAX Order（实际顺序）

期望顺序：`load → migrate → prune → dedup → update → HISTORY_MAX trim → save`
实际顺序（以代码为准）：

1. `load_sent_history`：parse → migrate(legacy) → **prune** (`prune_expired_entries`) → 返回存活 dict。
2. `dedupe_leads`：对传入 history 再 **prune**（defensive, line 1211）→ 判定。
3. `save_sent_history`：`load`(已 prune) → upsert → **prune** (line 1189) → **HISTORY_MAX trim** (line 1191) → write。

**关键点：** 在 `save_sent_history` 中，`prune_expired_entries()`（line 1189）**先于** `HISTORY_MAX` 容量裁剪（line 1191）执行。

✅ 过期 entries **会**在 HISTORY_MAX 容量计算之前被清理，不会占用 capacity。无此顺序缺陷。

---

## F. Legacy Migration（旧格式迁移）

旧格式 `['lead1','lead2']` 与 `{"urls":[...]}` 由 `_migrate_legacy()`（line 1118）处理：

1. ✅ 旧 cache 可正常读取（list / dict 两分支）。
2. ✅ 不会 crash（非 str 元素跳过；整体在 `load_sent_history` 的 try/except 保护内）。
3. ✅ 不丢 key（每个 url → 一个 dict key；dict 天然去重，无重复 key）。
4. ✅ 不产生重复 key。
5. ✅ 首次 `save_sent_history` 后落盘为 version 2 结构。
6. `first_seen` / `last_seen` 默认值 = **迁移/读取时刻（`now`）**（line 1135）。
7. ✅ migration 后的 entry 含 `last_seen`，TTL 逻辑正常生效。
8. ✅ migration 不破坏 `HISTORY_MAX`（trim 按 `last_seen`；所有迁移项时间戳相同，仅当 >2000 时保留最近 2000）。

**Trade-off（明确报告）：**
旧 cache **没有真实 timestamp**，当前代码把 `last_seen` 设为**迁移时刻**。含义：
- 所有旧 Lead 会获得一个**全新的 30 天压制窗口**（从迁移当天起算）。
- 它们既不会立即过期重发（避免缓存清空后的邮件洪泛），也不会永久阻塞（30 天后可重新进入）。
- 代价：原本可能"早已该重新出现"的旧 Lead，会被额外压制最多 30 天。

此 trade-off 是**可接受的设计取舍**（介于"立即重发"与"永久阻塞"之间最安全的折中），评级为 **LOW-RISK**。

---

## G. Cache Format

实际落盘格式（`save_sent_history`, line 1195）：

```json
{ "version": 2, "ttl_days": 30, "entries": { "<key>": { "first_seen": 1.7e9, "last_seen": 1.7e9 } } }
```

- **version**：有写入；但加载分派依据是 `"entries" in data`（line 1153），而非 version 数值。version 当前为**信息字段**。若未来出现 version 3，仍按 v2 格式解析（可能误判）。→ INFO 级，非 bug。
- **ttl_days**：有写入；但加载/判定时**未回读**文件中的 ttl_days 来覆盖运行时值。有效 TTL = `DISCOVERY_DEDUP_TTL_DAYS` 环境变量 / 常量（line 1139/1170 默认参数）。文件中的 ttl_days 为**元数据记录**。→ INFO 级，非 bug。
- **env 覆盖**：✅ `DISCOVERY_DEDUP_TTL_DAYS` 在模块加载时读取，覆盖默认值。
- **invalid/missing ttl fallback**：N/A（TTL 来源是 env/default，非文件）。
- **timestamp parsing**：`float(...)` 包 try/except（line 1158-1162）；非法时间戳 entry 被忽略。✅ 健壮。
- **timezone**：全程使用 `time.time()`（Unix epoch float），无 naive/aware datetime 比较。`generated_at` 显示用 `datetime.now(tz=GMT+8)` 仅用于报告字符串，**不参与 TTL 计算**。✅ 无时区比较异常风险。

---

## H. Environment Variable

`DISCOVERY_DEDUP_TTL_DAYS`（line ~176）：

```python
DISCOVERY_DEDUP_TTL_DAYS = int(os.getenv("DISCOVERY_DEDUP_TTL_DAYS", "30"))
```

- **默认值**：`30`。
- **读取位置**：模块级配置区（import 时执行）。
- **类型转换**：`int()`。
- **invalid value handling**：
  - 非数字（如 `"abc"`）→ `int("abc")` 抛 `ValueError` → **模块 import 即失败** → 整个 workflow 崩溃。属**运维误配置**才会触发，评级 **LOW-RISK**（建议未来改为带安全 fallback 的整型解析，但不在 A1 范围）。
  - `TTL = 0` → `age >= 0` 恒真 → 所有 entry 立即过期 → **完全不做去重**（每天重发）。退化但**不崩溃**。
  - `TTL < 0` → `age >= 负数` 恒真 → 同上，无去重。退化但**不崩溃**。
  - 缺失 → 默认 `30`。✅

---

## I. GitHub Actions Persistence

`daily_leads.yml` 在 A1 中**未改动**（已 `git diff --stat` 确认无变化）：

```yaml
- name: Cache sent history (dedup across runs)
  uses: actions/cache@v4
  with:
    path: sent_cache.json
    key: sent-history-cache-v1
```

- ✅ A1 保持了 `actions/cache` 跨 workflow run 持久化。
- **cache key**：`sent-history-cache-v1`（静态，无 `restore-keys`）。
- **path**：`sent_cache.json`（与 `HISTORY_FILE` 默认一致）。
- **不会产生新的 cache key**：key 静态，所有 run（定时 + 手动 `workflow_dispatch`）共用同一 key。
- **旧 cache 永远可恢复**：静态 key 保证每次 run 恢复的是同一缓存；不会因 key 变化导致旧 cache 无法恢复。
- **不会多 run 用不同 cache**：key 固定，无分支/日期变量。
- 若缓存因首次运行或 GitHub 保留期淘汰而缺失 → run 从空缓存启动并重建，符合预期。

✅ GitHub Actions 持久化正常，A1 TTL 机制建立在持久化缓存之上，行为正确。

---

## J. Dedup Position in Discovery Pipeline

实际顺序（`main()`）：

```text
Search
 → raw result
 → filter_blacklist
 → filter_competitors            (B 同行闸门)
 → filter_directory_listings     (B4 目录 Listing 过滤)
 → clean_with_ai
 → apply_post_ai_gates           (B2 同行 + 买方闸门，确定性权威)
 → load_sent_history             (读缓存)
 → dedupe_leads                  (A1 TTL 去重)
 → enrich_leads_with_emails / filter_competitor_emails
 → score_lead / generate_cold_email
 → send_email
 → save_sent_history (if sent)   (仅成功发送才写缓存)
```

- ✅ A1 dedup 发生在 **Phase B quality gates 之后**，仅合格线索进入 dedup。
- ✅ `save_sent_history` 仅在 `send_email` 成功后执行（line 1474 `if sent:`）。

**风险核查：** A1 **不会**在 Lead 未经过 Phase B gates 前就写入 history。被同行/买方闸门拒绝的坏供应商结果**永远不会**进入 `sent_cache`，因此**不会出现**"bad supplier → cached → 30 天 suppression → 错误占用 dedup lifecycle"的情况。

✅ Dedup 位置正确，无此风险。

---

## K. Metrics Availability

**结论：`NOT AVAILABLE`（结构化持久化指标）。**

- 代码中**无** `discovery_metrics.json` / funnel 落盘逻辑（grep 仅命中 `test_phase_b.py` 的测试函数名，非实现）。
- 现有**仅内存 + 打印**的计数（不持久化）：
  - 目录 Listing 过滤丢弃数：`filter_directory_listings` 打印。
  - 后闸门同行/买方丢弃数：`apply_post_ai_gates` 的 `comp_drop` / `buyer_drop`。
  - 去重丢弃数：`dedupe_leads` 的 `dropped`。
  - AI 重试/回退：既有日志。
- 这些计数仅在 Actions 运行日志可见，**未写入产物/缓存**，无法跨 run 聚合分析 recall。

（建议未来单独 Phase 增加 `discovery_metrics.json` 落盘 funnel；不在 A1 范围，本报告不添加。）

---

## L. Test Result（只读运行）

```text
$ pytest -q
........................................................................ [ 59%]
..................................................                       [100%]
122 passed in 3.33s
```

- **122 passed / 0 failed**。
- 构成：既有测试（含 Phase B）106 + 新增 A1 测试（`tests/test_phase_a1.py`）16；另有 2 个既有测试随 dict 返回类型最小化适配（计入 106）。
- **无 regression**，测试数量与预期一致。未修改任何测试以"凑通过"。

---

## M. Risks

| ID | 等级 | 描述 | 是否阻断 |
|---|---|---|---|
| R1 | LOW | 旧 cache 无真实 timestamp → `last_seen` 设为迁移时刻 → 旧 Lead 获全新 30 天压制窗口（可接受取舍，见 §F） | 否 |
| R2 | LOW | `DISCOVERY_DEDUP_TTL_DAYS` 非数字时 `int()` 在 import 期抛 `ValueError` 致整 run 崩溃（仅运维误配置触发） | 否 |
| R3 | INFO | `version` 字段为信息性（分派基于 `"entries"` 键）；有效 TTL 来自 env 而非文件 ttl_days | 否 |
| — | — | 未发现：错误阶段写缓存 / 搜索即刷 last_seen / TTL 未生效 / cache 无法跨 run / migration 丢数据 / 时区比较异常 / HISTORY_MAX 误淘汰 | — |

---

## N. Final Verdict

```text
PASS WITH LOW-RISK NOTE
```

**判定依据：**
- ✅ TTL 语义正确（sliding TTL，基于 `last_seen`，`age >= TTL` 过期）。
- ✅ `last_seen` 刷新时机合理——**仅成功发送的合格线索**刷新；search result / 被拒线索从不刷新，**无无限 sliding suppression 风险**（审计重点已排除）。
- ✅ 过期顺序正确——`prune` 在 `HISTORY_MAX` 容量计算之前执行，过期项不占 capacity。
- ✅ `HISTORY_MAX` 正常——已从"永久历史上限"转为"active TTL cache 尺寸上界"，按 `last_seen` 最旧优先淘汰，无 off-by-one / 误淘汰。
- ✅ Legacy migration 安全——可读、不崩溃、不丢 key、生成 v2；trade-off 已记录且可接受。
- ✅ GitHub Actions persistence 正常——`actions/cache` 未改，静态 key 保证跨 run 恢复。
- ✅ Dedup 位置正确——仅合格线索进入，且只在成功发送后写缓存；坏供应商不会被缓存。
- ✅ 无 regression（122 passed / 0 failed）。

**LOW-RISK NOTE（可接受，无需在 A1 修复）：**
1. 旧 cache 迁移给旧 Lead 一个全新 30 天压制窗口（§F / R1）。
2. `DISCOVERY_DEDUP_TTL_DAYS` 非数字值会在 import 期崩溃（§H / R2）——建议未来改为带 fallback 的安全整型解析。
3. `version` 为信息字段、有效 TTL 来自 env（§G / R3）——非缺陷。

> 本审计仅报告，未做任何修改。A1 保持 working tree 未提交 / 未推送；未进入 A2。
