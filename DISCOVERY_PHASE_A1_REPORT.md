# Discovery Phase A1 — 去重 TTL 生命周期化报告

> 目标：把 `sent_cache.json` + `HISTORY_MAX` + `actions/cache` 造成的**永久去重**
> 改造成**有生命周期的 TTL 去重**，在不取消 dedup 的前提下，避免每日有效 Lead 数量
> 随运行次数永久收敛。
>
> 范围约束：本 Phase **仅**修改去重 / 缓存序列化 / TTL 配置 / 过期清理 / 测试 / 文档。
> 未触碰 `lead_score`、阈值、同行规则、买方闸门、7 类分类、搜索关键词、搜索量、
> Bing `freshness`、分页、深爬等。

---

## A. 当前去重架构（改造前）

原始实现（`main.py` 历史去重模块）为**永久去重**：

1. **缓存文件**：`HISTORY_FILE`（默认 `sent_cache.json`），JSON 落盘。
2. **旧版格式**：一个**扁平 URL 集合**——`save_sent_history()` 写入 `json.dump(sorted(set))`，
   即 `["url1","url2",...]`（也兼容 `{"urls":[...]}` 形态）。
3. **读取**：`load_sent_history()` 返回 `set[url]`（文件缺失/损坏返回空集合）。
4. **去重键**：`source_url`（原始 URL 字符串）作为稳定去重键。
5. **去重判定**：`dedupe_leads(leads, history)` 中 `if url and url in history: drop`。
6. **写入时机**：`main()` 仅在邮件**发送成功**后调用
   `save_sent_history([l.get("source_url") for l in leads])`，把本次 URL 并入缓存。
7. **跨运行持久化**：`.github/workflows/daily_leads.yml` 用 `actions/cache@v4`
   （key `sent-history-cache-v1`）在每次 workflow run 之间恢复 `sent_cache.json`。

**调用链**：

```
load_sent_history()
   → set[url]                         # 无时间戳
→ dedupe_leads(leads, history)
   → url in history ? drop : keep
→ (AI 清洗 / 评分 / 发信)
→ send_email() 成功
→ save_sent_history(urls)
   → set 合并 + 截断 HISTORY_MAX
→ actions/cache 持久化 → 下次 run 恢复
```

---

## B. 根因（为什么造成长期 suppression）

三个因素叠加，使已发现 Lead **事实永久**不再出现：

1. **缓存无时间戳**：`sent_cache.json` 只存 URL 集合，没有 `first_seen` / `last_seen`。
   系统无法判断"这条线索是昨天发现的还是半年前发现的"。
2. **`HISTORY_MAX=2000` 只限尺寸、不限时效**：`save_sent_history` 仅在超过 2000 条时才
   从**最早**开始淘汰。而每日新增可推 URL 通常只有个位数，2000 的容量意味着条目在
   实际时间轴上**几乎永远不会被淘汰**——等效于永久黑名单。
3. **`actions/cache` 跨 run 恢复**：缓存被持续恢复，去重状态在多次 cron 运行间累积。

结果：Lead 第一次被发现 → 写入缓存 → 后续每次运行都判定"已发送/已处理" → 即使该买家
再次发出新的 RFQ，只要 URL 相同或相似，就被永久压制 → 每日 Unique 可推空间逐日收敛，
这是日更 cron 的**头号 recall 杀手**。

---

## C. 新 TTL 设计

引入配置化 TTL，把"永久去重"改为"生命周期去重"：

- **新增配置**：`DISCOVERY_DEDUP_TTL_DAYS`（环境变量，默认 **30**）。
  定义：同一线索在 TTL 天内重复出现 → 去重；超过 TTL → 允许重新进入 pipeline。
- **缓存 entry 结构**升级为含时间戳的字典：

  ```json
  {
    "version": 2,
    "ttl_days": 30,
    "entries": {
      "<lead_key>": { "first_seen": <unix_ts>, "last_seen": <unix_ts> }
    }
  }
  ```

- **边界规则**：`age = now - last_seen`；`age >= TTL` ⇒ **过期** ⇒ 允许重新发现。
  （即 TTL 当天仍视为过期，保守侧不包含边界日。）
- **写入语义**：新键 `first_seen = now`；已存在键仅刷新 `last_seen = now`
  （保留首次发现时间，便于后续分析）。
- **去重判定**：`dedupe_leads` 先对缓存做过期裁剪，再判定 `key in 存活键集`；
  无 `source_url` 的线索始终视为新（维持原规则）。

---

## D. 缓存兼容性（旧格式不破坏、不清空）

`load_sent_history()` 按形态分派：

| 磁盘形态 | 处理 |
|---|---|
| 文件缺失 / 损坏 / 非法 JSON | 返回 `{}`（绝不崩溃） |
| 新版 `{"version":2,"entries":{...}}` | 解析；非法 entry（非 dict / 缺 `last_seen` / 时间戳非法）逐个忽略 |
| 旧版 `list` | 迁移为 entry dict，`last_seen = 读取时刻` |
| 旧版 `{"urls":[...]}` | 同列表迁移 |

**迁移关键**：旧 entry 的 `last_seen` 被设为**迁移/读取时刻（reference time）**，使其获得
一个全新的 TTL 窗口——既不会立即过期（保留近期保护），也不会永久阻塞（TTL 后自然放行）。
旧格式在首次 `save_sent_history` 后被自然升级为新格式，**不丢失、不清空**历史。

---

## E. 过期处理

- **裁剪时机**：`load_sent_history` 加载后即时裁剪过期 entry；`save_sent_history` 写入前
  再防御性裁剪一次。内存中的活跃缓存**只含存活记录**，过期键不再阻塞对应线索。
- **清理函数**：`prune_expired_entries(entries, ttl_days, now)` 独立可测，返回仅含存活项的
  新 dict；非法/损坏 entry 一律丢弃。
- **尺寸保护**：`HISTORY_MAX` 仍生效，但职责调整为"存活缓存的尺寸上界"——超出时按
  `last_seen` 最旧优先淘汰（而非永久历史库）。形成 **TTL 过期 + 有界尺寸** 双重约束。
- **`actions/cache` 保留**：`daily_leads.yml` 的 `actions/cache@v4` 未改动，跨 run 持久化
  能力保留；只是持久化的内容现在是带 TTL 的缓存，过期条目会被自然清理，不再永久阻塞。

---

## F. 测试

| 类别 | 数量 |
|---|---|
| 既有测试（含 Phase B） | 106 |
| 新增 A1 测试（`tests/test_phase_a1.py`） | 16 |
| **合计** | **122** |
| **通过** | **122** |
| **失败** | **0** |

A1 新增测试覆盖（对应需求 Test 1–10 + 支撑用例）：

1. `test_fresh_entry_deduped` — 当天 entry 必去重
2. `test_within_ttl_deduped` — TTL 内（10d<30d）必去重
3. `test_ttl_boundary_expired` — 边界（age==TTL）过期、允许重新进入
4. `test_expired_entry_rediscoverable` — 过期（31d）不去重、可重入
5. `test_expired_entry_cleanup` — 过期 entry 从活跃缓存清理
6. `test_different_keys_independent` — 不同 key 互不干扰
7. `test_same_key_within_ttl_deduped` — 同 key TTL 内重复必去重
8. `test_legacy_list_format_loadable` / `test_legacy_urls_dict_format_loadable` — 旧格式可读取
9. `test_corrupt_cache_no_crash` / `test_invalid_entry_in_new_format_ignored` — 损坏/非法 entry 不崩溃
10. `test_history_max_bounds_size` — `HISTORY_MAX` 仍限制尺寸
11. `test_default_ttl_is_30` — 默认 TTL = 30 天
12. `test_ttl_roundtrip_persistence` — 新格式落盘 + 重载一致
13. `test_save_refreshes_last_seen_keeps_first_seen` — 刷新 last_seen、保留 first_seen
14. `test_lead_without_url_never_deduped` — 无 URL 线索永不去重（维持原规则）

> 注：`tests/test_resilience.py` 中 2 个既有测试（`test_history_roundtrip`、
> `test_load_history_missing_returns_empty`）随新 dict 返回类型做了最小化适配，
> 行为语义不变，仍计入 106 既有测试。

---

## G. 范围审计（Scope Audit）

明确确认本 Phase 未修改以下任何一项：

| 项目 | 状态 |
|---|---|
| `lead_score` / 评分权重 | ✅ 未改 |
| thresholds（阈值） | ✅ 未改 |
| competitor 规则 | ✅ 未改 |
| buyer gate（买方闸门） | ✅ 未改 |
| 7 类公司分类 | ✅ 未改 |
| 搜索关键词 / `KEYWORD_GROUPS` | ✅ 未改 |
| 搜索量（`SEARCH_PER_KEYWORD` / `RESULTS_LIMIT`） | ✅ 未改 |
| Bing `freshness` | ✅ 未改 |
| 分页 / 深爬 | ✅ 未改 |
| AI 分类 / Opportunity / Intent Engine / CRM | ✅ 未改 |

仅修改：`sent_cache` 去重逻辑、缓存序列化/反序列化、TTL 配置、过期清理、相关测试、README 文档。

---

## H. Git 状态

```text
# Phase B 已提交（HEAD）
4eeea58 fix(discovery): harden lead precision gates

# A1 工作树改动（未提交 / 未推送）
 M README.md
 M main.py
 M tests/test_resilience.py
?? tests/test_phase_a1.py
```

- Phase B：已 commit（`4eeea58`），未 push（按指令）。
- A1：仅留 working tree changes，**未 commit、未 push**。
- 无无关修改；`.github/workflows/daily_leads.yml`（`actions/cache`）未触碰。

---

## 最终状态确认

```text
HEAD
└── Phase B commit (4eeea58)

Working tree
└── A1 changes only (README.md, main.py, tests/test_resilience.py, tests/test_phase_a1.py)

A1
└── tested (122 passed / 0 failed)
└── audited (scope OK)
└── NOT committed
└── NOT pushed
```

后续如需进入 Phase A2（数量放量，如提升搜索量/放宽 freshness），请另开指令。
