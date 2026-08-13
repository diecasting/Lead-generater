# Daily Lead Collector（每日潜在客户线索自动搜集）

基于 **GitHub Actions** 的自动化 Leads 搜集与推送系统。每天（北京时间 08:00）自动运行一次，针对以下垂直行业在网络上寻找有采购意图的潜在客户，并汇总成美观的 HTML 日报，通过邮件推送到指定邮箱：

- **CNC 加工** (CNC machining)
- **压铸** (Die casting)
- **铸造** (Casting)
- **塑胶模具与注塑产品** (Plastic injection molding & parts)

## 工作原理

```
GitHub Actions (每天 UTC 00:00 / 北京 08:00)
        │
        ├─► 1. 多关键词搜索（含垂直黄页/社区定向搜索 site:thomasnet.com 等）
        │       ├─ Bing Web Search API（配置了 BING_API_KEY 时）
        │       └─ DuckDuckGo 公开搜索（未配置时的回退方案）
        │
        ├─► 2. 垃圾站点过滤
        │       剔除知乎/维基/博客/中介广告等，只保留真实买家线索
        │
        ├─► 3. AI 智能清洗过滤（失败自动回退规则清洗）
        │       抽取：客户/买家、需求摘要、来源网址、置信度
        │
        ├─► 4. 网页邮箱提取 + 意向评分(0-100) + 个性化英文开发信
        │
        └─► 5. 邮件推送（按意向分排序、🔥/⚡ 等级标签、含开发信草稿）
               经 SMTP (SSL 或 STARTTLS) 发送至 alumcastor@gmail.com
```

> 说明：若 `OPENAI_API_KEY` 未配置、额度不足（429）或网络异常，系统会自动回退到**基于关键词规则的本地清洗**，当天线索不会丢失；同一来源的线索会按 `sent_cache.json` 历史去重，避免重复推送。定向搜索、邮箱提取、意向评分与开发信均为纯本地逻辑（无需 API），即使零额度也能完整产出。

## 项目结构

```
.
├── .github/
│   └── workflows/
│       └── daily_leads.yml      # GitHub Actions 定时任务（每天 UTC 00:00 / 北京 08:00）
├── main.py                      # 核心执行脚本
├── requirements.txt             # Python 依赖
├── tests/                       # pytest 单元测试（配置校验 / 规则清洗 / 容灾 / 邮箱提取 / 去重）
├── sent_cache.json              # 已推送线索缓存（自动生成，已 gitignore，CI 中通过 cache 持久化；TTL 生命周期去重，格式为 {"version":2,"ttl_days":30,"entries":{...}}）
└── README.md                    # 本说明
```

运行后还会在 Actions 产物中生成 `leads_report.html` 与 `leads_report.json`。

## 本地运行（调试用）

```bash
pip install -r requirements.txt

export OPENAI_API_KEY="sk-..."
export MAIL_SERVER="smtp.gmail.com"
export MAIL_PORT="465"
export MAIL_USERNAME="you@example.com"
export MAIL_PASSWORD="your_app_password"
export MAIL_RECIPIENT="alumcastor@gmail.com"
# 可选：export BING_API_KEY="..."   # 不设置则使用 DuckDuckGo 回退

python main.py
```

## GitHub Secrets 配置清单

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 中添加以下变量。

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | OpenAI（或兼容端点）的 API Key，用于 AI 清洗过滤 |
| `OPENAI_BASE_URL` | ⬜ | 自定义/兼容端点。留空则使用官方 `api.openai.com`；可填 Azure OpenAI、OpenRouter 等 |
| `OPENAI_MODEL` | ⬜ | 模型名，默认 `gpt-4o-mini` |
| `BING_API_KEY` | ⬜ | Bing Web Search API Key（Azure 认知服务）。**不配置时自动回退到 DuckDuckGo 公开搜索** |
| `MAIL_SERVER` | ✅ | SMTP 服务器，例如 `smtp.gmail.com` |
| `MAIL_PORT` | ✅ | SMTP SSL 端口，通常为 `465` |
| `MAIL_USERNAME` | ✅ | 发件邮箱账号 |
| `MAIL_PASSWORD` | ✅ | 发件邮箱密码 / **应用专用密码**（见下方说明） |
| `MAIL_RECIPIENT` | ⬜ | 收件人，默认 `alumcastor@gmail.com` |
| `MAIL_SENDER` | ⬜ | 发件人显示名对应的邮箱，默认等于 `MAIL_USERNAME` |

> 兼容性说明：配置校验时 `MAIL_SERVER` 等价于 `MAIL_HOST`、`MAIL_USERNAME` 等价于 `MAIL_USER`，两套命名均可被识别，不影响运行。

### 关于邮箱密码的重要提示

- **Gmail**：不能使用普通登录密码，需在 Google 账号 → 安全 → 开启两步验证 → 生成 **App Password（应用专用密码）**，填到 `MAIL_PASSWORD`。Gmail 要求发件 `From` 与登录账号完全一致，脚本已自动把 `From` 对齐到 `MAIL_USERNAME`（即 `alumcastor@gmail.com`）；**请勿在 `MAIL_SENDER` 中填写与登录账号不同的地址**，否则会被 Gmail 拒绝（5.7.1 SendAsDenied）。只需把 `MAIL_USERNAME` / `MAIL_PASSWORD`（`MAIL_SERVER` 填 `smtp.gmail.com`、`MAIL_PORT` 填 `465`）配置为 Gmail 应用专用密码即可正常发信。
- **企业邮箱（如腾讯企业邮 / 阿里邮箱 / SiteGround / Outlook）**：填写对应的 SMTP 地址与授权码。常见地址：
  - 腾讯企业邮：`smtp.exmail.qq.com`，端口 `465`
  - SiteGround 企业邮：`sgp14.siteground.asia`，端口 `587`（脚本已内置 STARTTLS 支持）
  - Outlook/Hotmail：`smtp.office365.com`，端口 `587`（脚本已内置 STARTTLS 支持）
- 端口 `465` 走隐式 SSL；端口 `587`（或任意非 465 端口）走 `STARTTLS` 加密升级，脚本会根据端口自动选择，无需手动改代码。
- 若不配置任何 SMTP 凭据，脚本不会报错退出，而是仅把报告保存为本地产物，方便先验证搜索与 AI 流程。

## 自定义搜索关键词

搜索关键词以分类矩阵 `KEYWORD_GROUPS`（位于 `main.py` 顶部）组织，分为「压铸与模具类」「CNC 与精密加工类」「外贸与代工买家类」三组，共 30 个长尾/采购意图词。编辑该字典即可增删关键词；每组内的关键词会分别发起一轮搜索。

如需进一步扩大搜索量，可开启**组合搜索**（见下方「环境变量 / 配置项」中的 `SEARCH_COMBINE`）。

## 环境变量 / 配置项（非敏感，可选）

以下变量均为**非敏感配置**，可通过环境变量传入（本地运行时 `export`，或 GitHub Actions 的 `env:` / `vars`）。未设置时使用默认值，均可正常运行。

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `SEARCH_PER_KEYWORD` | `10` | 每个关键词搜索返回的结果条数上限 |
| `SEARCH_COMBINE` | `0` | 是否开启组合搜索。`1` 时把「工艺词 × 意图词」交叉组合生成更多长尾查询（受 `SEARCH_COMBINE_MAX` 限制），扩大搜索覆盖；`0` 关闭 |
| `SEARCH_COMBINE_MAX` | `8` | 组合搜索生成的最大查询数量（防止查询爆炸） |
| `RESULTS_LIMIT` / `LEADS_LIMIT` | `20` | 每次运行最终收集并推送的线索数量上限（两名为同义，任一生效即可） |
| `EMAIL_EXTRACTION` | `1` | 是否开启网页邮箱自动抓取。`0` 关闭（仅依赖搜索结果中的邮箱）；`1` 开启后会轻量抓取线索网页正文提取邮箱 |
| `EMAIL_MAX_FETCH` | `5` | 单次运行最多抓取的网页数量（控制耗时与请求量） |
| `EMAIL_FETCH_TIMEOUT` | `8` | 单条网页抓取的超时时间（秒） |
| `AI_MAX_RETRIES` | `2` | 大模型调用遇到 429 / 网络错误时的最大重试次数 |
| `AI_RETRY_BASE_DELAY` | `3` | 重试退避基准秒数，第 n 次重试等待 `base × 2^(n-1)`（即 3s → 6s） |
| `OPENAI_BASE_URL` | 空（官方） | 兼容端点地址（Azure / OpenRouter / 本地 vLLM 等） |
| `OPENAI_MODEL` | `gpt-4o-mini` | 使用的模型名 |
| `HISTORY_FILE` | `sent_cache.json` | 已推送线索历史缓存文件路径 |
| `HISTORY_MAX` | `2000` | 历史缓存保留的最大条目数（超出后按最近 `last_seen` 淘汰最旧条目） |
| `DISCOVERY_DEDUP_TTL_DAYS` | `30` | 去重 TTL（天）。同一线索在 TTL 天内重复出现则去重；超过 TTL 允许重新进入 discovery pipeline（生命周期去重，替代原永久去重） |
| `DISCOVERY_SEARCH_FRESHNESS` | `Week` | Bing 搜索时间窗（freshness）。允许值：`Day` / `Week` / `Month`。**仅作用于配置了 `BING_API_KEY` 的 Bing 搜索路径**；DDG 回退路径不支持 freshness 参数，配置对其无效。非法值会打印警告并回退到 `Week`，discovery 不会崩溃 |
| `DIRECTORY_SEARCH` | `1` | 是否开启垂直黄页 / 专业社区定向搜索（`site:` 限制）。`0` 关闭，仅做普通全网搜索 |
| `DIRECTORY_SITES` | `thomasnet.com,kompass.com,reddit.com/r/manufacturing,engineering.com,globalspec.com` | 定向搜索的站点白名单（逗号分隔）。系统会把关键词与这些站点轮询配对，生成 `关键词 site:站点` 查询，精准捕获黄页 / 社区的高价值线索 |
| `DIRECTORY_MAX_QUERIES` | `12` | 定向搜索（site:）生成的最大查询数量，用于控制额外请求量 |

### 本地调试示例（含新增配置项）

```bash
pip install -r requirements.txt

export OPENAI_API_KEY="sk-..."
export MAIL_SERVER="sgp14.siteground.asia"
export MAIL_PORT="587"
export MAIL_USERNAME="you@your-domain.com"
export MAIL_PASSWORD="your_mail_auth_code"
export MAIL_RECIPIENT="alumcastor@gmail.com"

# 可选：开启组合搜索 + 调整线索上限
export SEARCH_COMBINE=1
export RESULTS_LIMIT=25
export EMAIL_EXTRACTION=1

python main.py
```

## 定时时间说明

GitHub Actions 的 `cron` 使用 **UTC**。北京时间为 UTC+8，因此「北京时间 08:00」对应 `0 0 * * *`。如需调整为其他时间，按「北京时间 − 8 小时 = UTC」换算后修改 `.github/workflows/daily_leads.yml` 中的 cron 表达式即可。

## 注意事项

- 免费额度有限：Bing 搜索 API 与 OpenAI API 均按调用量计费，请关注用量。
- AI 过滤依赖大模型判断，结果仅供参考，建议人工复核高价值线索后再跟进。
- 抓取公开网页请遵守目标站点 `robots.txt` 与使用条款，仅用于合法 B2B 线索挖掘。
