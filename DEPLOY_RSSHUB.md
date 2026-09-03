# RSSHub 部署与接入

RSSHub 独立部署在 Vercel，本项目只读取它输出的 RSS/Atom。访问密钥只放在 Vercel 环境变量和 GitHub Actions Secrets，绝不提交到仓库。

## 1. Fork RSSHub

1. 登录 GitHub，打开 <https://github.com/DIYgod/RSSHub>。
2. 点击右上角 **Fork**，保留默认设置，Fork 到自己的个人账号。

## 2. 生成访问密钥

在本机 PowerShell 执行：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

复制输出的随机串。不要把它发到聊天、提交到 Git，或写进 `config/feeds.yaml`。

## 3. 部署到 Vercel

1. 打开 <https://vercel.com>，选择 **Continue with GitHub** 登录。
2. 点击 **Add New… → Project**，Import 刚才 Fork 的 `RSSHub`。
3. **Framework Preset** 选择 `Other`。RSSHub 已带 `vercel.json`，不要改 Build Command、Output Directory 或 Install Command。
4. 在 **Environment Variables** 添加：

   | Name | Value |
   | --- | --- |
   | `ACCESS_KEY` | 上一步生成的随机串 |
   | `CACHE_EXPIRE` | `3600` |
   | `NODE_ENV` | `production` |

5. 点击 **Deploy**。完成后复制形如 `https://rsshub-xxx.vercel.app` 的 Production URL。

## 4. 验证 RSSHub

浏览器打开（替换域名和密钥）：

```text
https://rsshub-xxx.vercel.app/zhihu/hotlist?key=你的_ACCESS_KEY
```

成功时页面是 XML，并包含 `<rss`、`<feed` 或订阅条目。出现 401/403 时检查 `key` 是否与 Vercel 的 `ACCESS_KEY` 完全一致；出现 404 时先在 RSSHub 文档确认路由是否仍有效。

## 5. 接入 GitHub Actions

进入 `hot-gap-aggregator` 仓库：**Settings → Secrets and variables → Actions → Secrets → New repository secret**，新增：

- `RSSHUB_BASE`：Production URL，例如 `https://rsshub-xxx.vercel.app`，末尾不加 `/`
- `RSSHUB_KEY`：与 Vercel `ACCESS_KEY` 完全相同的随机串

然后进入 **Actions → Collect, build and publish → Run workflow**。成功后检查构建日志中的 `source_finished`：`feed` 应为 `ok`，并确认网站出现「AI动态」「工具更新」两个 Tab。

## 6. 保持 RSSHub 更新

进入自己 Fork 的 RSSHub 仓库首页，看到落后于上游时点击 **Sync fork → Update branch**。Vercel 会监听 GitHub 更新并自动重新部署。

## 7. 以后增加订阅源

1. 去 <https://docs.rsshub.app> 搜索目标网站并找到路由。
2. 复制路由路径，例如 `/github/release/scverse/scanpy`。
3. 在 `config/feeds.yaml` 的 `feeds:` 下增加一项，设置 `tab`、`translate` 和 `limit`。
4. `git commit`、`git push`；下一次 GitHub Actions 会自动采集。

示例：

```yaml
- name: 新订阅源
  route: /example/route
  tab: ai       # hot / ai / papers / tools / jobs
  translate: true
  limit: 10
```

X-MOL 路由中的 `<待填magazine_id>` 是占位符，采集器会主动跳过。先从 x-mol.com 对应期刊页取得数字 ID，再替换占位符。

## 免费额度与路由限制

- Vercel Hobby 适合个人、非商业项目。当前官方说明包含每月最多约 100 GB Fast Data Transfer；本项目每两小时读取十几个小型 XML feed，正常远低于这个量。
- 新项目通常启用 Fluid Compute，Hobby Function 当前默认/最高时长可到 300 秒；未启用 Fluid Compute 的旧配置默认可能仍是 10 秒、最高 60 秒。具体以 Vercel 项目 **Settings → Functions** 显示为准：<https://vercel.com/docs/functions/limitations>。
- 即使时长足够，需要登录、验证码或无头浏览器渲染的 RSSHub 路由仍可能失败。优先使用普通 HTTP 抓取的轻量路由；采集器会跳过单个失败路由，不影响网站其它来源。
- Hobby 当前典型 Fast Data Transfer 指引为 100 GB/月：<https://vercel.com/docs/limits/fair-use-guidelines>。超限时免费项目可能暂停，而不是自动收费。

## 本地验证（可选）

不要把密钥写进命令历史；建议放在被 `.gitignore` 忽略的 `.env`：

```dotenv
RSSHUB_BASE=https://rsshub-xxx.vercel.app
RSSHUB_KEY=你的_ACCESS_KEY
FEEDS_CONFIG=config/feeds.yaml
```

随后运行：

```powershell
python -m app.run
```

检查 `public/data/ai.json`、`public/data/tools.json`，以及合并了 RSSHub 条目的 `papers.json`、`jobs.json`。
