# Discovery Quality Audit — 只读审计报告

> 范围：`main.py` + `lead_filter_engine.py` + `.github/workflows/daily_leads.yml`
> 约束：未修改任何代码、未提交、未改 `lead_score` / threshold / sales_priority / Opportunity / Intent Engine（这些模块本仓库不存在）。
> 说明：本沙箱无网络、无 GitHub Actions 运行历史，**无法读取真实生产日志/缓存**。本文中"最近 7 天数量"为基于代码逻辑推演 + 可测量方案，并非真实计数。

---

## 一、10 个问题的直接回答

### 1. 当前 Discovery 使用了哪些 search keywords？
定义在 `main.py:76` `KEYWORD_GROUPS`，**全部为买方视角**，4 组 × 10 = **40 个基础关键词**：
- 压铸与铸造买家：`looking for die casting supplier` / `need custom aluminum die casting parts` / `aluminum die casting RFQ buyer` / `request for quote die casting` / `outsourcing die casting production` / `seeking zinc die casting manufacturer` / `custom product development die casting buyer` / `want to source die cast components` / `sand casting buyer inquiry` / `aluminum die casting sourcing inquiry`
- 塑胶模具与注塑买家（10 个，含 `OEM plastic mold RFQ` / `looking for injection molding supplier` 等）
- CNC 与精密加工买家（10 个，含 `seeking CNC machining supplier` / `looking for precision machining partner` 等）
- 外包与 OEM/ODM 采购买家（10 个，含 `product development company seeking supplier` / `engineering company sourcing machined parts` 等）

附加搜索（见 `main.py:341` `get_directory_queries`）：
- `DIRECTORY_SEARCH` 默认开启（`main.py:183`），用 `DIRECTORY_MAX_QUERIES=12`（`main.py:184`）生成 **12 条 `site:` 定向查询**，轮询打到 `thomasnet.com / kompass.com / reddit.com/r/manufacturing / engineering.com / globalspec.com`。
- `SEARCH_COMBINE` **默认关闭**（`main.py:150`），若开启最多再加 8 条组合长尾。

**默认每次运行实际搜索 ≈ 40 + 12 = 52 条查询。**

### 2. 每个 keyword 最近产生多少结果？
- 单关键词抓取量由 `SEARCH_PER_KEYWORD=10`（`main.py:162`）控制 → Bing `count=10` / DDG `[:count]`。
- 即**每个关键词最多 10 条原始结果**，52 条查询理论最多 ≈ 520 条原始结果/天（运行内对相同 URL 去重后更少）。
- **真实每关键词计数无法在此环境获得**（需运行 + 抓 Actions 日志）。报告末尾给出可测量方案。

### 3. 当前 lead_score / confidence_score / qualification threshold 分别是多少？
- **confidence**（非数值）：`high / medium / low`，来自规则清洗或 AI 判定（`main.py:562`）。
- **score_lead（0–100）**（`main.py:874`）：基础分 `high=40 / medium=25 / low=10`；+20 有邮箱；+10 含企业域名邮箱；材质/参数/图纸/数量各 +8（上限受文本命中）；高价值域名 +10；封顶 0–100。
- **tier**（`main.py:902`）：`>=70 🔥高意向` / `>=45 ⚡中意向` / 其余 `💤低意向`。
- **真正的合格闸门（qualification gate）** 在规则路径（`main.py:552`）：
  `(TRUE_BUYER_RE 或 RFQ_PLATFORM_RE 命中) 且 ad < 2 且 noise == 0`。
  另有 `RULE_MIN_SCORE = 3`（`main.py:492`）但被买方闸门实质覆盖。
- AI 路径无硬阈值：LLM 自行判定后 `leads[:RESULTS_LIMIT]` 直接返回。

### 4. 当前是否已区分 BUYER / SUPPLIER / COMPETITOR / OEM / DISTRIBUTOR / SERVICE_PROVIDER / IRRELEVANT？
**否。** 系统只有二元判定，没有 7 类 taxonomy：
- `is_competitor()` → 二元"是同行则丢弃"（`lead_filter_engine.py:165`）
- `is_true_buyer()` / `TRUE_BUYER_RE` / `RFQ_PLATFORM_RE` → 二元"是买方则保留"
- `confidence` 只是 high/medium/low，**不是公司类型**；`score_lead` 是意向分，**不是类型**
- **没有** OEM / DISTRIBUTOR / SERVICE_PROVIDER / IRRELEVANT 任何标签或字段。
- 每条线索的输出字段仅有：`company / need_summary / source_url / keyword / confidence / emails / score / cold_email`。

### 5. 当前系统如何判断一家公司是潜在采购客户？
- **规则路径**：标题+摘要+关键词拼接后必须命中 `TRUE_BUYER_RE`（`lead_filter_engine.py:222`，如 `we are looking for`/`seeking a supplier`/`our company needs`/`need to outsource`/`requesting a quote`）**或** `RFQ_PLATFORM_RE`（`rfq`/图纸/CAD/STEP 等），且 `ad<2`、`noise==0`。
- **AI 路径**：依赖 `SYSTEM_PROMPT`（`main.py:427` 附近）指示模型丢弃供应商、保留买方意图页；**AI 返回后没有再强制跑买方闸门**。
- 制造工艺识别 `matched_capabilities()`（`main.py:911`）仅用于开发信个性化，**不参与资格判定**。

### 6. 当前如何排除 die casting supplier / casting manufacturer / competitor？
两道（均不完整）：
- **文本同行过滤** `is_competitor()`（`lead_filter_engine.py:165`）应用在 `raw` 上（`main.py:1212`，AI 之前）：
  - 硬短语 `COMPETITOR_HARD_PHRASES`（`lead_filter_engine.py:118`）：`we are a manufacturer` / `our foundry` / `casting capabilities` / `machining services provider` / `injection molding supplier` / `iso certified factory` …
  - 正则 `COMPETITOR_REGEX`（`lead_filter_engine.py:144`）：要求 `we/our + 供应商名词` 或 `X services (provider|company|supplier)`。
- **邮箱同行过滤** `filter_competitor_emails()`（`main.py:1227`）：仅当一条线索提取到的邮箱**全部**是同行邮箱且正文非买方时才丢弃。
- **缺口**：见 C 节——对 "Aluminum Die Casting **Services**" / "Die Casting **Supplier**" / "Casting **Manufacturer**" 这类**通用供应商标题/描述**基本漏判（详见 C）。

### 7. 最近 7 天发现的 Leads 中 buyer / competitor / supplier / irrelevant 各有多少？
**无法在此环境给出真实数字**：`sent_cache.json` 未被纳入仓库（`git ls-files` 无 cache/report/history），Actions 运行产物（artifact）也不在本沙箱。
可测量方案（见 G）：在 `main.py` 各环节打印并落盘每轮 funnel 计数（raw → blacklist → competitor → buyer_gate → dedup → final）+ 每条线索的 `classify_company()` 标签，连续运行 7 天后即可精确统计。**当前只能定性推断**：每日仅 ~1 条终稿，而该 1 条被反馈为"Die Casting Supplier"型，说明 competitor 在终稿中占比偏高（可能 ≥ 多数天数），real buyer 极少，irrelevant 多在中间漏斗被滤掉。

### 8. 哪些 search keywords 产生最多 competitor？
- **所有含名词 `supplier / manufacturer / die casting / CNC machining / injection molding` 的查询**都会吸引供应商主页（搜索引擎按词义匹配，供应商页面大量使用这些词）。
- **12 条 `site:` 定向查询是最大 competitor 源**：`thomasnet / kompass / globalspec / engineering.com` 本身就是**供应商目录**，搜索 "looking for die casting supplier site:thomasnet.com" 返回的几乎都是**供应商黄页 listing（即竞争对手）**，而非买家。这是悖论：本意是找买家，却从供应商目录里捞。
- 相对少 competitor 的：含第一人称买方动词的查询（`looking for…` / `buyer inquiry` / `sourcing inquiry` / `product development company seeking supplier` / `engineering company sourcing…` / `distributor looking for custom parts`）——但仍会有供应商页面混进来。

### 9. 哪些 keywords 最有可能产生真正 Buyer？
按"买家页语言特征"排序（命中 `TRUE_BUYER_RE`/`RFQ_PLATFORM_RE` 概率高）：
1. `aluminum die casting RFQ buyer` / `CNC machining RFQ buyer` / `plastic injection molding buyer inquiry` / `OEM CNC milling buyer`（含 RFQ + buyer 词）
2. `product development company seeking supplier` / `engineering company sourcing machined parts` / `distributor looking for custom parts`（OEM/分销商/产品开发公司第一人称）
3. `looking for die casting supplier` / `looking for precision machining partner`（第一人称 looking for）
4. 社区类 `reddit.com/r/manufacturing` 偶发真实求助帖。
本质上**不是关键词本身决定 buyer，而是 snippet 是否含第一人称买方动词**——上述查询的 snippet 更容易出现这类措辞。

### 10. 当前为什么每日只有 ~1 个 Lead？
四重叠加（按贡献从大到小）：
1. **去重缓存随时间膨胀**：`sent_cache.json` 经 `actions/cache` 跨运行持久化（`daily_leads.yml:41`，key `sent-history-cache-v1`），`HISTORY_MAX=2000`（`main.py:171`）。一旦 URL 被推送即**永久**不再出现。每日新增可推 URL 的"未覆盖空间"逐日缩小。
2. **Bing `freshness="Week"`**（`main.py:274`）：只取近 7 天内容，把大量稍旧但仍有效的 RFQ 排除。
3. **买方闸门过严**：`TRUE_BUYER_RE`/`RFQ_PLATFORM_RE` 要求 snippet 含显式第一人称买方动词或 RFQ 标记；搜索摘要普遍偏短，真实买家常写间接语（"need a partner for our new product"、"who can make this?"）而漏判。
4. **抓取深度/广度有限**：`SEARCH_PER_KEYWORD=10` 单页、无翻页、无正文深抓；目录查询又大量灌入 competitor 稀释 buyer 产出。

→ 漏斗：`~520 原始` → 黑名单 → 同行文本过滤（部分漏） → **买方闸门（绝大部分被丢）** → 去重（历史重叠）→ 最终常剩 1–5 条，其中 1 条进日报且常为 competitor。

---

## A. 当前 Discovery Pipeline（漏斗）

```
[每日 00:00 UTC / 北京 08:00, cron]
   │
   ├─ collect_raw_leads()                         # 52 查询 × 10 = ≤520 原始
   │     ├─ 40 买方关键词（Bing f reshness=Week / DDG 回退）
   │     └─ 12 site: 定向（thomasnet/kompass/reddit/engineering/globalspec）
   │
   ├─ filter_blacklist()                          # 知乎/维基/博客/中介广告
   ├─ filter_competitors()  ◄── 仅此处跑 is_competitor（AI 之前）
   │
   ├─ clean_with_ai()  ──► 若 OPENAI 有 key：LLM 判定返回 leads[:20]
   │                     └─ 否则回退 clean_with_rules()（含买方闸门）
   │        ⚠  AI 输出【不再】跑 is_competitor / passes_buyer_gate
   │
   ├─ dedupe_leads(history)   # sent_cache.json（actions/cache 持久化）
   ├─ enrich_leads_with_emails()
   ├─ filter_competitor_emails()  # 仅当全部邮箱是同行才丢
   ├─ score_lead() 0–100 + tier + cold_email
   ├─ build_html_report() → leads_report.html/json
   └─ send_email() ── 成功才 save_sent_history()
```

关键代码锚点：`main.py:1193 main()`、`collect_raw_leads:356`、`clean_with_ai:590`、`clean_with_rules:514`、`dedupe_leads:1100`、`score_lead:874`。

---

## B. 当前最大质量瓶颈

**精度瓶颈 > 数量瓶颈**，且二者相互加剧：
- **最大瓶颈 = 竞争对手泄漏（precision）**：`is_competitor` 的模式覆盖有缺口（见 C），且**AI 输出后没有再跑同行/买方闸门**，导致漏网的 competitor 与 LLM 误判的 supplier 直接进日报——这正是"每日 1 条却是 Die Casting Supplier"的根因。
- 次要瓶颈 = 去重+新鲜度把**数量**压到 1（见 D）。
- 两个瓶颈叠加：先因闸门过严把真 buyer 也滤掉（数量低），又因同行过滤缺口把 competitor 放进来（质量低），最终"少且错"。

---

## C. Competitor False Positive 根因

1. **`is_competitor` 模式覆盖缺口（主因）**
   - 硬短语只认 `we are a manufacturer` / `our foundry` / `injection molding supplier` 等**完整句式**；对通用供应商标题/描述**不命中**：
     - `Aluminum Die Casting Services` → 无 "we are"、无 "services provider/company/supplier" 后缀 → `is_competitor=False`
     - `Die Casting Supplier` → 硬短语只有 `injection molding supplier`，无 `die casting supplier` → `False`
     - `Casting Manufacturer` / `Precision Machining Company` / `Metal Parts Fabricator` → 缺 "we are/leading/we specialize" 等触发词 → `False`
   - 正则 `COMPETITOR_REGEX` 要求 `X services (provider|company|supplier)`，而 "Die Casting Services" 后无 provider/company/supplier → 不命中。
2. **AI 输出无二次闸门**：`clean_with_ai()` 返回后直接进入去重/邮箱/评分，**没有再跑 `is_competitor` 或 `passes_buyer_gate`**。初始 `filter_competitors` 已漏的 competitor，只要 LLM 也误判为 buyer，就进日报。
3. **目录 site: 查询是 competitor 温床**：`thomasnet/kompass/globalspec` 是供应商目录，其返回天然是 competitor listing；而 `is_competitor` 对纯 listing 标题（"ABC Die Casting – Supplier"）覆盖不足。
4. **邮箱过滤器触发条件过窄**：`filter_competitor_emails` 仅当"全部邮箱都是同行"才丢；供应商页若留 `info@`/`contact@` 或无邮箱则不触发。

---

## D. Recall（数量）问题根因

1. **去重缓存跨运行永久化 + HISTORY_MAX=2000**：`sent-history-cache-v1` 使已推 URL 永不重现，可推 Unique 空间逐日收敛——日更 cron 的头号 recall 杀手。
2. **Bing `freshness="Week"` 硬编码**（`main.py:274`）：排除 >7 天内容，许多仍有效的 RFQ 被砍。
3. **买方闸门过严且仅看短 snippet**：`TRUE_BUYER_RE` 必须第一人称买方动词或 RFQ 标记；真实买家间接表述 + 摘要过短 → 漏判。
4. **无深度/无翻页**：`SEARCH_PER_KEYWORD=10` 单页；不抓正文，买方动词常出现在正文中而非 snippet。
5. **关键词-web 错配**：买方意图查询仍大量返回供应商页（web 按词义匹配），净 buyer 产出/关键词低。

---

## E. 推荐的 Discovery Quality Upgrade 方案

> 两套方案均**不动 `lead_score` / 任何 threshold / sales_priority / Opportunity / Intent Engine**（本仓库亦无后者）。

### 方案 A — 提高 Lead 数量（Recall）
- 去重改为 **TTL/轮转**：`sent_cache` 仅压制 N 天（如 30 天）或按关键词轮转，避免 Unique 空间永久收敛。
- `freshness` 改为可配置（`FRESHNESS` env，默认 `Month` 或关闭），放宽候选池。
- 提高 `SEARCH_PER_KEYWORD`（如 20–30）+ 翻页；开启 `SEARCH_COMBINE` 做组合长尾。
- **深抓正文**：搜索后 fetch 前 N 条页面正文，用更长文本跑买方闸门（间接买方语也能命中）。
- 扩充买方意图信号：在 `TRUE_BUYER_RE` 增加 "who can manufacture" / "recommend a supplier" / "need quotes" / 采购招标(tender) 等。
- 增加 RFQ 社区/黄页源（LinkedIn、Quora、行业论坛、专门 RFQ 板）。

### 方案 B — 提高 Lead 质量（Precision，不动阈值）
- **扩展 `is_competitor`**：新增"通用供应商描述"规则——标题/域名以 `Supplier / Manufacturer / Factory / Foundry / Services / -ing Company` 结尾；域名含 `diecast/casting/cnc/machining/mold` 且非已知品牌；"we supply/we provide/we offer" 且无买方动词。覆盖 `Aluminum Die Casting Services` / `Die Casting Supplier` 等。
- **AI 输出后强制二次闸门**：在 `main()` 中 `clean_with_ai` 返回后**再跑一次 `filter_competitors` + `passes_buyer_gate`**，LLM 误判的 competitor 被兜底拦截（最高杠杆、最低风险）。
- **新增 7 类公司分类器** `classify_company()`：`BUYER / SUPPLIER / COMPETITOR / OEM / DISTRIBUTOR / SERVICE_PROVIDER / IRRELEVANT`，作为线索字段；日报可展示类型，便于你人工筛选。
- **目录 listing 规则**：源域名属供应商目录（thomasnet 等）且正文无买方动词 → 直接丢。
- 拆分"制造工艺检测"与"采购信号检测"为独立维度，供分类器与评分共用（当前 `matched_capabilities` 仅用于邮件，未参与资格）。

### ✅ 推荐先做哪套
**先做方案 B（质量），再做方案 A（数量）。**
理由：当前痛点核心是"那 1 条是 competitor"。若先放大量（方案 A）而不先补 precision，会把更多 competitor 灌进日报，雪上加霜。**B 的"AI 输出二次闸门 + 扩展 is_competitor"几乎零风险、不动阈值、即日可降 false positive**；质量可信后，再放宽度（A）才有意义。

---

## F. 建议新增/修改的文件（仅建议，本次未改）

| 文件 | 改动 | 目的 |
|---|---|---|
| `lead_filter_engine.py` | 扩展 `COMPETITOR_HARD_PHRASES`/`COMPETITOR_REGEX` 覆盖通用供应商描述；新增 `classify_company()` 7 类；`filter_leads` 增加最终二次闸门选项 | B 精度 |
| `main.py` | `clean_with_ai` 返回后**再跑** `filter_competitors` + `passes_buyer_gate`；`bing_search` 的 `freshness` 改为 `FRESHNESS` env；`dedupe` 增加 TTL/轮转；为正文深抓与 `classify_company` 接入 | A+B |
| `main.py` | 打印并落盘每轮 funnel 计数 + 每条 `classify_company` 标签（JSON） | 可观测性（解 Q7） |
| `tests/test_lead_filter_engine.py` | 新增：通用供应商标题判 competitor、7 类分类、AI 输出二次闸门、每关键词 competitor 率回归 | G |
| `README.md` | 文档化新增 env（`FRESHNESS` / `DEDUP_TTL` / `SEARCH_PER_KEYWORD` 行为 / `classify_company`） | 运维 |
| （新增）`discovery_metrics.py` | 可选：把 funnel + 分类计数聚合成 7 天报表 | 监控 |

---

## G. 测试方案

**单元（precision，锁死 competitor 泄漏）**
- `is_competitor("Aluminum Die Casting Services")` → `True`
- `is_competitor("Die Casting Supplier")` → `True`
- `is_competitor("Casting Manufacturer")` → `True`
- `is_competitor("Precision Machining Company")` → `True`
- `classify_company` 对上述返回 `SUPPLIER`/`COMPETITOR`，对 "We are looking for a die casting supplier" 返回 `BUYER`
- **AI 输出二次闸门**：构造 `clean_with_ai` 误返回的 competitor lead，断言 `main()` 终稿不含它。

**集成（recall 回归 + 可观测）**
- 每关键词 funnel 测试：断言「同行率」不高于基线（防止关键词改坏精度）。
- 注入真实 buyer 间接表述（"need a partner for our new product"），断言深抓正文后能命中买方闸门（防过度严格）。
- 端到端冒烟：混合 competitor + 反向邀约 + 真实 buyer，断言 competitor 全剔除、buyer 保留。

**生产可观测（解 Q7 "最近 7 天各类多少"）**
- 在 `main()` 各环节 `print` 并写 `discovery_metrics.json`：`{date, raw, after_blacklist, after_competitor, after_buyer_gate, after_dedup, final, per_keyword_competitor: {...}, per_type: {buyer, supplier, competitor, oem, distributor, service_provider, irrelevant}}`。
- 连续运行 7 天 → 即得真实 buyer/competitor/supplier/irrelevant 计数，无需猜测。
