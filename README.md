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
        ├─► 1. 多关键词搜索
        │       ├─ Bing Web Search API（配置了 BING_API_KEY 时）
        │       └─ DuckDuckGo 公开搜索（未配置时的回退方案）
        │
        ├─► 2. AI 智能清洗过滤
        │       调用大模型 API，剔除新闻/教程/供应商自广告等噪声，
        │       抽取：客户/买家、需求摘要、来源网址、置信度
        │
        └─► 3. 邮件推送
               生成 HTML 日报，经 SMTP (SSL) 发送至 Hank@alumcasting.com
```

## 项目结构

```
.
├── .github/
│   └── workflows/
│       └── daily_leads.yml      # GitHub Actions 定时任务
├── main.py                      # 核心执行脚本
├── requirements.txt             # Python 依赖
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
export MAIL_RECIPIENT="Hank@alumcasting.com"
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
| `MAIL_RECIPIENT` | ⬜ | 收件人，默认 `Hank@alumcasting.com` |
| `MAIL_SENDER` | ⬜ | 发件人显示名对应的邮箱，默认等于 `MAIL_USERNAME` |

### 关于邮箱密码的重要提示

- **Gmail**：不能使用普通登录密码，需在 Google 账号 → 安全 → 开启两步验证 → 生成 **App Password（应用专用密码）**，填到 `MAIL_PASSWORD`。
- **企业邮箱（如腾讯企业邮 / 阿里邮箱 / Outlook）**：填写对应的 SMTP 地址与授权码。常见地址：
  - 腾讯企业邮：`smtp.exmail.qq.com`，端口 `465`
  - Outlook/Hotmail：`smtp.office365.com`，端口 `587`（此时需改用 STARTTLS，可联系维护者调整脚本）
- 若不配置任何 SMTP 凭据，脚本不会报错退出，而是仅把报告保存为本地产物，方便先验证搜索与 AI 流程。

## 自定义搜索关键词

编辑 `main.py` 顶部的 `KEYWORDS` 列表即可增删目标行业关键词。每个关键词会分别发起一轮搜索。

## 定时时间说明

GitHub Actions 的 `cron` 使用 **UTC**。北京时间为 UTC+8，因此「北京时间 08:00」对应 `0 0 * * *`。如需调整为其他时间，按「北京时间 − 8 小时 = UTC」换算后修改 `.github/workflows/daily_leads.yml` 中的 cron 表达式即可。

## 注意事项

- 免费额度有限：Bing 搜索 API 与 OpenAI API 均按调用量计费，请关注用量。
- AI 过滤依赖大模型判断，结果仅供参考，建议人工复核高价值线索后再跟进。
- 抓取公开网页请遵守目标站点 `robots.txt` 与使用条款，仅用于合法 B2B 线索挖掘。
