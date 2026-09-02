# Codex 实施提示词 · 小红书商家规则/品类冻结预警

在 `hot-gap-aggregator` 里加一个**独立的监控管线**：每隔几小时用商家账号 cookie 抓小红书千帆后台的「自家商品状态 + 规则中心 + 商家公告」，和上一次快照做 diff，变动条目交给大模型判断「是否涉及虚拟商品 / 电子资源 / 激活码 / 知识付费 等品类的售卖限制、类目要求、资质、冻结下架规则，个人店是否受影响」，命中就推**飞书**告警。

**背景**：店主卖公考激活码（虚拟商品）。小红书会突然收紧招商/合规规则、把某些品类改成"需邀约、个人店不可发布"，命中后直接冻结链接、只给 2 天申诉窗口。上一次就是「虚拟商品未在指定类目发布」被冻结。需要在规则变动当天甚至更早收到预警。

---

## 现状（Codex 先读）

- 仓库是**刚初始化、尚未 push** 的本地 git 仓库，分支 `main`。直接在 `main` 上做，禁止 worktree / 建分支。
- Python 3.11+ / Playwright 已是依赖（`app/collectors/xiaohongshu.py` 已用 headless chromium 抓关键词雷达）。
- `app/notify.py`：已有 bark / telegram / serverchan 三个 provider 和 `_post()` 重试助手；`gongkao_push_log` 表 + `unseen_gongkao_events()` / `mark_gongkao_events()` 是**去重推送的现成范式**，照抄。
- `app/store/database.py`：SQLite（`data/hot.db`），`_migrate()` 里 `CREATE TABLE IF NOT EXISTS` 追加即可。
- 翻译/摘要走智谱 `glm-4-flash`（`POST https://open.bigmodel.cn/api/paas/v4/chat/completions`），key = `ZHIPU_API_KEY`；也支持 OpenAI 兼容（`OPENAI_API_KEY/BASE_URL/MODEL`）。
- `.github/workflows/publish.yml` 是每 2h 的采集+构建+部署；`notify.yml` 是每日 Top20。两个都用 `git-auto-commit-action` 把 `data/hot.db` 提交回去。
- `.env` / `.env.*` 已被 `.gitignore` 排除。所有真实 key 只进 GitHub Secrets。

---

## 【发给 Codex 的提示词】

```
你在给 hot-gap-aggregator 加一个独立的「小红书商家规则/品类冻结预警」监控管线。这是合规预警，不是热榜采集——不要把它塞进 app/run.py 的热榜流程，单独一个入口、单独一个 workflow。

## 协作纪律
- 仓库是刚初始化未 push 的本地仓库，分支 main。直接在 main 上做，禁止 git worktree / 建分支 / 切分支。
- 不碰 app/collectors/**、app/pipeline/**、前端 web/**、publish.yml、部署配置。你只新增 app/xhs_rules/ 包、给 app/notify.py 加飞书 provider、加一个新 workflow、加 config 示例和测试。
- 不提交 .env、cookie、任何真实 key。
- 分两个阶段交付（见下）。做完 git commit，不 push。

## 目标
每 3 小时（可配）用商家 cookie 抓小红书千帆后台，diff 出新增/变更条目，大模型判断相关性，命中推飞书。

## 一、数据来源（都在千帆商家后台，带 cookie 访问）
按信号强度优先做：
1. **自家商品状态**（最高信号）——商品管理里的「审核驳回」和「已冻结」列表。每条：商品名、状态、驳回/冻结原因全文、时间。能直接告诉店主"你的商品 X 被冻结，原因 Y"。
2. **平台规则中心 / 规则更新**——规则列表页，每条规则的标题、更新时间、生效时间、详情页 URL、正文（进详情页抓）。
3. **商家公告 / 消息中心**——平台公告列表，标题+时间+正文。
千帆的确切域名和路径你用店主给的 cookie 实际登录后确认（可能是 ark.xiaohongshu.com / pgy.xiaohongshu.com / 商家后台 h5）。抓不到某个来源就降级跳过，不要让整个管线崩。

## 二、新增 app/xhs_rules/ 包
- `watcher.py`：
    - 读 `XHS_MERCHANT_COOKIE`（原始 Cookie 头字符串 `k1=v1; k2=v2`），解析成 Playwright cookie 对象（domain `.xiaohongshu.com`），`context.add_cookies()`。
    - headless chromium，真实 UA，页面间隔 1–2s，每类来源单独 try/except。
    - 每类来源解析成结构化条目 `RuleEntry(section, entry_key, title, url, published_at, body_text)`：
        - `section` ∈ {"product_rejected","product_frozen","rule_center","announcement"}
        - `entry_key` = 稳定标识（商品 id / 规则 doc id / 公告 id；没有就 `sha256(section+url+title)[:16]`）
        - `body_text` = 规范化后的正文纯文本（去空白、去脚本）
    - **cookie 失效检测**：被重定向到登录页 / 出现扫码登录 / 关键接口 401 → 抛 `SourceUnavailable(status="degraded")`，且让上层发一条飞书告警「小红书 cookie 已失效，需要重新抓取粘贴到 Secret」。宁可吵也不要静默死掉。
    - 选择器要有多重兜底；结构完全对不上时降级并告警，不 crash。
- `store.py`（或直接加到 app/store/database.py 的 _migrate）：
    ```
    CREATE TABLE IF NOT EXISTS xhs_rule_entries (
      entry_key TEXT PRIMARY KEY, section TEXT NOT NULL, title TEXT NOT NULL,
      url TEXT, content_hash TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS xhs_rule_push_log ( event_key TEXT PRIMARY KEY, pushed_at TEXT NOT NULL );
    ```
    - diff：库里没有该 entry_key → `new`；有但 content_hash 变了 → `changed`；一样 → 跳过。
    - `event_key = f"{entry_key}:{content_hash[:12]}"`，每个不同版本只推一次（照 gongkao_push_log 的 unseen/mark 写法）。
- `classify.py`：
    - 只对 new/changed 条目调大模型（省钱）。复用现有 LLM 配置：优先 `ZHIPU_API_KEY` + glm-4-flash，其次 OpenAI 兼容。直接写一个 async httpx 调用即可，不必接 Translator 类。
    - 关注品类清单放 `config/xhs_watch.yaml`（示例见下），prompt 里带上。
    - 每条返回 JSON：`{relevant: bool, categories_hit: [str], severity: "high"|"medium"|"low", one_line: str, action_hint: str}`。
      - `product_frozen` / `product_rejected` 一律 relevant=true、severity 至少 medium（这是店主自己的商品出事了）。
      - 规则/公告类：只有涉及虚拟商品/电子资源/激活码/知识付费/账号/软件/教程/资料 等的售卖限制、类目调整、资质门槛、冻结下架规则，且个人店可能受影响，才 relevant=true。
    - LLM 失败 → 保守处理：relevant=true, severity="low", one_line="模型判断失败，请人工看原文"。
- `__main__.py`：`python -m app.xhs_rules`
    - `--once`（默认）跑一轮；`--dry-run` 不推送只打印。
    - 流程：load_dotenv → 抓三类来源 → diff → 对 new/changed 分类 → 相关的组装告警 → `notify_xhs_rules()` → 成功推送后 mark event_keys → 更新 xhs_rule_entries 的 content_hash/last_seen → 打结构化日志（照 app/run.py 的 log_event 风格）。
    - cookie 失效那条告警独立于分类流程，一定要发。

## 三、飞书 provider（改 app/notify.py）
- 新增读取 `FEISHU_WEBHOOK`（自定义机器人 webhook）和可选 `FEISHU_SECRET`（开了加签时）。
- 加签：`timestamp + "\n" + secret` 用 HMAC-SHA256，base64，作为 `sign` 字段；body `{"timestamp","sign","msg_type":"text","content":{"text": ...}}`。没配 secret 就不带这两个字段。
- 把飞书也加进现有 `notify_top20` 的 provider 列表（这样每日 Top20 也能发飞书，顺带的）。
- 新函数 `async def notify_xhs_rules(alerts: list[XhsAlert], database) -> dict[str,str]`：
    - 每条 alert 一段文本：
        ```
        🚨 小红书规则预警 · {severity_label}
        【{section_label}】{title}
        命中品类：{categories_hit}
        {one_line}
        建议动作：{action_hint}
        原文：{url}
        抓取时间：{ts}
        ```
    - 多条合并成一条消息（≤ 飞书上限），按 severity 高→低排序。
    - 只发 relevant=true 的；relevant=false 的只写进日志/DB，不推。
    - 走 `_post()`，飞书失败标 degraded 不 crash。

## 四、Workflow：.github/workflows/xhs-rules.yml
- `on: schedule: cron "0 */3 * * *"` + `workflow_dispatch`
- `permissions: contents: write`
- steps：checkout → setup-python 3.11 → pip install -r requirements.txt → `python -m playwright install --with-deps chromium` → 跑 `python -m app.xhs_rules`，env：
    ```
    XHS_MERCHANT_COOKIE: ${{ secrets.XHS_MERCHANT_COOKIE }}
    FEISHU_WEBHOOK: ${{ secrets.FEISHU_WEBHOOK }}
    FEISHU_SECRET: ${{ secrets.FEISHU_SECRET }}
    ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
    XHS_WATCH_CONFIG: config/xhs_watch.yaml
    ```
  → `git-auto-commit-action@v5`，`file_pattern: "data/hot.db"`，`continue-on-error` 或按 publish.yml 的 best-effort rebase 写法。

## 五、config/xhs_watch.example.yaml（同时给 config/xhs_watch.yaml 一份真实的）
```yaml
# 命中这些关键词/品类的规则变动才推送
watch_categories:
  - 虚拟商品
  - 电子资源
  - 激活码 / 兑换码
  - 知识付费 / 课程 / 教程 / 资料
  - 账号 / 会员代充
  - 软件 / 工具 / 网络工具
  - 数字商品 / 卡密
individual_shop: true   # 店铺是个人店（受"需邀约类目"影响最大）
```

## 六、.env.example 追加
```
# 小红书商家规则监控
XHS_MERCHANT_COOKIE=
FEISHU_WEBHOOK=
FEISHU_SECRET=
XHS_WATCH_CONFIG=config/xhs_watch.yaml
```
README 加一节：怎么拿 cookie（浏览器 F12 → Network → 任一千帆后台请求 → 复制整个 Cookie 请求头；或 Application → Cookies 全选导出），粘进本地 `.env` 或 GitHub Secret `XHS_MERCHANT_COOKIE`；cookie 会过期，收到"cookie 失效"飞书告警就重新弄一次。

## 阶段一（不需要 cookie，先交这个）
- app/xhs_rules/ 全部代码 + 飞书 provider + workflow + config 示例 + .env.example/README。
- 解析函数吃**离线 fixture**（你先按你对千帆页面结构的合理假设造 HTML/JSON fixture 放 tests/fixtures/xhs/）。
- pytest（照 tests/ 现有风格，纯离线）：
    - 解析 fixture → 正确产出 RuleEntry 列表
    - diff：new / changed / unchanged 三种判定正确
    - 去重：同 event_key mark 后 unseen 为空
    - 分类：product_frozen 强制 relevant；给一段"虚拟商品类目调整"文本 mock LLM 返回 relevant=true
    - 飞书 payload：不带 secret / 带 secret（验证 sign 字段存在且是 base64）
    - cookie 失效路径：watcher 抛 SourceUnavailable，__main__ 会调用 cookie 失效告警
- `pytest` 全绿、`ruff`/项目 lint 通过。
- git commit（`feat(xhs-rules): merchant rule + category-freeze watcher`）。
- 贴给我：新增文件清单、`python -m app.xhs_rules --dry-run` 在没 cookie 时的输出（应优雅报"缺 XHS_MERCHANT_COOKIE"或"cookie 失效"并尝试发飞书）、测试输出。

## 阶段二（我把 cookie 和飞书 webhook 给你之后）
- 用真实 cookie 跑 `python -m app.xhs_rules --dry-run`，把三类来源页面的真实 DOM/JSON 存成 fixtures，据此修正选择器/解析。
- 跑一次真实 `--dry-run` 打印会推送的告警，确认命中判断合理（尤其"已冻结"那条要能抓到并识别为 high）。
- 配好 `FEISHU_WEBHOOK` 后跑一次真实推送，截图飞书收到的消息。
- 更新 fixtures + 测试，git commit。
- 贴给我：真实 dry-run 输出、飞书截图、最终测试结果。

## 有疑问先问我
```

---

## 说明（给店主）

- **两层信号**：第 1 类「自家商品驳回/冻结」是事后但最快最准——链接一出事你立刻知道原因；第 2、3 类「规则中心/公告」是事前——规则文本一改就预警，抢在批量冻结前调整。
- cookie 会过期（一般几天到两周），失效时管线会主动发飞书叫你重新贴，不会闷声失灵。
- 频率默认 3 小时一次；要更频繁把 cron 改 `0 */1 * * *`。
- 目前只盯小红书；以后要加抖音小店 / 视频号小店，同一套 watcher + 分类 + 飞书结构复制即可。
- 这个管线不影响热榜聚合和静态站，独立 workflow、独立 SQLite 表、独立 Secret。
