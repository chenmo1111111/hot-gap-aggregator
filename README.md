# 信息差日报 · hot-gap-aggregator

每两小时聚合微博、B站、GitHub、YouTube、抖音、Telegram、公考、小红书关键词雷达、论文、岗位、会议 Deadline 和牛客热帖，把非中文内容翻译为中文，以“先看标题、感兴趣再点开”的方式浏览。前端是无运行时外部依赖的静态 PWA；通用源由 GitHub Actions 采集，国家公务员局、五省选调与城市人才补贴监控在中国大陆服务器运行。

## P2 能力

- 智谱 `glm-4-flash` 默认翻译，并为 GitHub/YouTube 生成“这是什么 + 为什么值得看”的 40 字内点评；翻译和摘要均按 SHA-256 + provider 缓存。
- YouTube 有官方 Key 时走 Data API；无 Key 时依次轮询 `config/invidious_instances.yaml`，合并 US/JP/GB 热门视频。
- 小红书只做关键词雷达：Playwright 无头浏览器按关键词搜索、点赞排序、每词前 15；页面改版或拦截时单源降级。
- 趋势页展示上升最快、今日新晋、跌出榜单、霸榜王，普通卡片附最近 7 天手写 SVG 排名线。
- 公考支持省份多选、类型筛选、四类时间节点、倒计时、34 个已验证官方入口和节点推送去重。
- 首页置顶至少 3 个平台共同出现的聚类；每个来源单独显示正常/降级状态。
- 顶刊论文合并 arXiv、bioRxiv/medRxiv 和 PubMed，按个人研究方向、兴趣关键词和发表时间排序。
- 中文核心通过 Crossref 按 7 个已核对 ISSN 采集，顶刊页按英文顶刊、中文核心、预印本分区。
- 岗位雷达逐关键词查询腾讯与字节的单细胞/AI4Science 职位，同名岗位合并，另提供 BioMap、深势、晶泰和华为官网直达。
- 顶刊页顶部展示生信/ML 会议 Deadline；牛客通过 RSSHub best-effort 聚合并按秋招风险词和目标公司排序。
- 国考/选调支持快捷筛选、跨省强提醒和目标高校标红；国家公务员局官方专题源在百度云交叉校验。
- 百度云每 12 小时监听目标地区人社局公告；标题命中补贴关键词的新条目即时预警。另对 3 个核心政策页做正文 hash diff，只有金额、条件、窗口或名额变化才由模型判断并推送。
- 百度云每 6 小时监听黑龙江、辽宁、河北、天津、山东官方选调公告；东北林业大学/东北林大/NEFU 命中项最高优先级单独推送，并合入公考「选调生」筛选。
- 多用户账号系统通过签名 HttpOnly Cookie 保护线上数据，每个账号的导航、主题与筛选偏好可跨设备同步；管理员可在设置面板建号、删号和重置密码。

## 本地运行

需要 Python 3.11+ 和 Node.js。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env

python -m app.run
# 另一个终端启动账号 API（需先在 .env 配 ADMIN_USER、ADMIN_PASSWORD、SESSION_SECRET）
uvicorn sync.app:app --host 127.0.0.1 --port 8787
npm install
npm run dev
```

采集产物仍写入 `public/data/*.json`；线上由 Nginx 鉴权后提供，账号与偏好 API 由轻量 Python 服务负责。
线上账号与 Nginx 部署见 [`DEPLOY_AUTH.md`](DEPLOY_AUTH.md)。本地 Vite 会把 `/api`
代理到 `127.0.0.1:8787`。

## 智谱翻译与摘要

本地 `.env`：

```dotenv
TRANSLATOR=zhipu
ZHIPU_API_KEY=在智谱后台新生成的_key
ENABLE_SUMMARY=true
```

接口为 `POST https://open.bigmodel.cn/api/paas/v4/chat/completions`，模型为 `glm-4-flash`。批次间隔至少 0.5 秒，失败重试 2 次，最终失败保留原文。更换 provider 后可清掉非智谱缓存并重翻：

```bash
python -m app.run --retranslate
```

也可把 `TRANSLATOR` 设为 `openai`、`free` 或 `none`。OpenAI 兼容模式读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。

`.env` 和 `.env.local` 已由 `.gitignore` 的 `.env*` 排除。任何 API Key 只放本地 `.env` 或仓库 Settings → Secrets and variables → Actions → Secrets；不要提交进 YAML 或代码。Actions 通过 `${{ secrets.ZHIPU_API_KEY }}` 注入。

## 来源配置

### YouTube / Invidious

- 配置 `YOUTUBE_API_KEY`：使用官方 API。
- 不配置：依次轮询 `config/invidious_instances.yaml`。
- `INVIDIOUS_BASE=https://example.com` 可强制只用一个自定义实例。
- 全部实例失败时 YouTube 标记为 `degraded`，不会阻断其他来源。

### Telegram

仓库已带 10 个公开科技/新闻频道的 `config/telegram_channels.yaml`；也可从 `.example.yaml` 复制后自行调整。频道公开链接通常是 `https://t.me/username`，其中最后一段就是 username；也可以在 Telegram 频道资料的分享链接中找到。

每个频道的 `translate_telegram: true` 表示正文进入翻译层，`false` 表示保留原文。私有频道或只有邀请链接的频道无法通过公开预览页采集。

### 小红书关键词雷达

编辑 `config/xhs_keywords.yaml`，每行一个关键词。它是搜索结果雷达，并非官方全站热榜。CI 会安装 Chromium；本地首次运行需执行 `python -m playwright install chromium`。

### 公考

- `config/gongkao_watch.yaml`：`provinces` 控制省份，`exam_types_alert` 中的国考/选调生不受省份限制，`target_universities` 用于定向选调标红。
- `data/gongkao_official_sites.yaml`：全国 34 个官方人事考试入口。
- `python scripts/verify_official_sites.py`：重新检查入口 HTTP 状态。
- 国家公务员局旧首页已于 2026 年 8 月下线；服务器任务从官方专题站公开 JSON 接口动态发现当年考试 ID，每两小时补充公告、报名和大纲信息。

### 顶刊论文

- `config/papers.yaml`：配置研究方向、次级关键词、arXiv 类目、bioRxiv 类目过滤和 PubMed 期刊。
- `priority_topics` 按列表顺序分级；命中项优先于普通关键词和其它论文。
- bioRxiv、medRxiv、PubMed 均按最近 `lookback_days` 天采集；任一子源失败不会阻断其它子源。
- PubMed 无需 Key 即可使用；可选 `NCBI_API_KEY` 能提高 E-utilities 速率上限。
- `journals_by_issn` 是 Crossref 中文核心清单，窗口默认 45 天；只保留 `journal-article`，无摘要也保留标题。可选 `CROSSREF_MAILTO` 用于 Crossref polite pool 联系邮箱。

### 单细胞 / AI4Science 岗位

- `config/job_radar.yaml` 维护关键词、每词上限与无接口公司的官网直达。
- 腾讯 JSON API 与字节 JSON API 各自重试、独立降级；字节站的浏览器签名/风控可能返回 405，此时腾讯结果和直达按钮仍可用。
- 去重键为岗位名 + 公司；同一职位命中多个关键词会合并关键词并优先展示。

### 会议 Deadline

- `config/conferences.yaml`：维护会议简称白名单。
- YAML 首选 `huggingface/ai-deadlines`，并兼容持续更新的 CCFDDL 嵌套格式，`paperswithcode` 旧仓库作为末级回退；WikiCFP 生信 RSS 独立降级。
- 只保留未来或过去 7 天内的 Deadline，按剩余天数升序展示在「顶刊」顶部。

### 牛客热帖

- `config/nowcoder.yaml`：`keywords` 放通用风险词，`companies` 填目标公司名。
- `RSSHUB_BASE` 可切换到自建 RSSHub；公共实例不可用时只把牛客标为降级。

### 人社局公告与人才补贴

- `config/subsidy_sources.yaml`：沈阳、石家庄、天津、德州、山东、河北、辽宁官方人社公告源，以及沈阳生活/购房、石家庄安家补贴核心政策页。
- 公告按 `(region, url)` 去重；首次运行只建立基线，之后标题命中关键词的新条目才提醒。
- 政策页正文变化后调用智谱/DeepSeek判断金额、条件、窗口和名额；模型返回 `SKIP` 时不提醒。
- 推送严格按飞书 → Bark 选一个渠道；都未配置时写入线上 `data/alerts.json`，前端「预警」标签显示未读红点。服务器发布会保留该文件。
- 百度云安装与 cron 见 `deploy/server/README.md`，SCS 每两小时、补贴监听每 12 小时。

### 五省选调公告

- `config/xuandiao_sources.yaml` 使用五个官方页面，顺序即黑龙江 > 辽宁 > 河北 > 天津 > 山东的优先级。
- 首次运行只建立 `(region, url)` 基线；之后新公告推送并写入公考 JSON，`extra.subsource=xuandiao`。
- 标题命中 `东北林业大学`、`东北林大` 或 `NEFU` 时标红并使用最高优先级飞书卡片；单站故障不会影响其它省份。

## 推送

```bash
python -m app.run --notify
```

支持配置 Bark (`BARK_URL`)、飞书自定义机器人 (`FEISHU_WEBHOOK`，开加签时再配 `FEISHU_SIGN_SECRET`)、Telegram Bot (`TG_BOT_TOKEN` + `TG_CHAT_ID`) 和 Server酱 (`SERVERCHAN_KEY`)。公考中，关注省份、重点考试类型或标题命中 `cities_focus` 任一条件都会触发新公告、报名前 1 天、截止前 2 天、笔试前 3 天提醒；仅在至少一个渠道发送成功后写入 push log 去重。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `TRANSLATOR` / `ZHIPU_API_KEY` | 翻译 provider 与智谱密钥 |
| `ENABLE_SUMMARY` | GitHub/YouTube 一句话点评，默认 `true` |
| `YOUTUBE_API_KEY` | 可选的 YouTube 官方 API Key |
| `INVIDIOUS_BASE` | 可选单实例覆盖 |
| `INVIDIOUS_INSTANCES_CONFIG` | Invidious 实例清单路径 |
| `TELEGRAM_CHANNELS_CONFIG` | Telegram 频道清单路径 |
| `XHS_KEYWORDS_CONFIG` | 小红书关键词路径 |
| `GONGKAO_WATCH_CONFIG` | 公考关注省份、城市和考试类型路径 |
| `PAPERS_CONFIG` / `NCBI_API_KEY` / `CROSSREF_MAILTO` | 论文配置、可选 PubMed Key 与 Crossref 联系邮箱 |
| `CONFERENCES_CONFIG` | 关注会议白名单路径 |
| `NOWCODER_CONFIG` / `RSSHUB_BASE` | 牛客关键词配置与 RSSHub 地址 |
| `JOB_RADAR_CONFIG` | 岗位关键词与公司直达配置 |
| `SUBSIDY_SOURCES_CONFIG` | 百度云人社公告与补贴政策页配置 |
| `XUANDIAO_SOURCES_CONFIG` | 百度云五省选调公告配置 |
| `BARK_URL` / `FEISHU_*` / `TG_*` / `SERVERCHAN_KEY` | 推送渠道 |
| `SERVER_DATABASE` / `SERVER_SITE_DATA_DIR` | 百度云 watcher 状态库与线上 JSON 目录 |

完整默认值见 `.env.example`。

## 验证与构建

```bash
pytest
npm run lint
npm run build
npm audit
```

生产构建输出到 `web/dist/`。网络逻辑单测均使用 fixture/mock，不访问真实站点。

## 上线部署

每两小时采集、构建、持久化历史并发布的配置见 [`DEPLOY.md`](DEPLOY.md)。推荐使用 Cloudflare Pages；仓库变量 `DEPLOY_TARGET` 可切换到 GitHub Pages。
