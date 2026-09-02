# 信息差日报 · hot-gap-aggregator

每两小时聚合微博、B站、GitHub、YouTube、抖音、Telegram、公考、小红书关键词雷达和顶刊论文，把非中文内容翻译为中文，以“先看标题、感兴趣再点开”的方式浏览。前端是无运行时外部依赖的静态 PWA；采集、SQLite 历史、趋势派生和推送由 Python 与 GitHub Actions 完成。

## P2 能力

- 智谱 `glm-4-flash` 默认翻译，并为 GitHub/YouTube 生成“这是什么 + 为什么值得看”的 40 字内点评；翻译和摘要均按 SHA-256 + provider 缓存。
- YouTube 有官方 Key 时走 Data API；无 Key 时依次轮询 `config/invidious_instances.yaml`，合并 US/JP/GB 热门视频。
- 小红书只做关键词雷达：Playwright 无头浏览器按关键词搜索、点赞排序、每词前 15；页面改版或拦截时单源降级。
- 趋势页展示上升最快、今日新晋、跌出榜单、霸榜王，普通卡片附最近 7 天手写 SVG 排名线。
- 公考支持省份多选、类型筛选、四类时间节点、倒计时、34 个已验证官方入口和节点推送去重。
- 首页置顶至少 3 个平台共同出现的聚类；每个来源单独显示正常/降级状态。
- 顶刊论文合并 arXiv、bioRxiv/medRxiv 和 PubMed，按个人研究方向、兴趣关键词和发表时间排序。

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
npm install
npm run dev
```

采集产物写入 `public/data/*.json`，前端直接读取，不需要 Python Web 服务。

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

- `config/gongkao_watch.yaml`：节点推送关注省份。
- `data/gongkao_official_sites.yaml`：全国 34 个官方人事考试入口。
- `python scripts/verify_official_sites.py`：重新检查入口 HTTP 状态。

### 顶刊论文

- `config/papers.yaml`：配置研究方向、次级关键词、arXiv 类目、bioRxiv 类目过滤和 PubMed 期刊。
- `priority_topics` 按列表顺序分级；命中项优先于普通关键词和其它论文。
- bioRxiv、medRxiv、PubMed 均按最近 `lookback_days` 天采集；任一子源失败不会阻断其它子源。
- PubMed 无需 Key 即可使用；可选 `NCBI_API_KEY` 能提高 E-utilities 速率上限。

## 推送

```bash
python -m app.run --notify
```

支持同时配置 Bark (`BARK_URL`)、Telegram Bot (`TG_BOT_TOKEN` + `TG_CHAT_ID`) 和 Server酱 (`SERVERCHAN_KEY`)。除 Top 20 外，关注省份会触发新公告、报名前 1 天、截止前 2 天、笔试前 3 天提醒；仅在至少一个渠道发送成功后写入 `gongkao_push_log` 去重。

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
| `GONGKAO_WATCH_CONFIG` | 公考关注省份路径 |
| `PAPERS_CONFIG` / `NCBI_API_KEY` | 论文配置路径与可选 PubMed API Key |
| `BARK_URL` / `TG_*` / `SERVERCHAN_KEY` | 推送渠道 |

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
