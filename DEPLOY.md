# 上线部署清单

推荐选择 **Cloudflare Pages**。它使用 `site/` 纯静态产物，在中国大陆通常比 GitHub Pages 更容易访问。下面两条路线已经共用同一个 `.github/workflows/publish.yml`，通过仓库变量 `DEPLOY_TARGET` 切换。

## 1. 在 GitHub 创建空仓库

1. 打开 GitHub，右上角 `+` → `New repository`。
2. Repository name 填 `hot-gap-aggregator`。
3. Public 或 Private 均可；不要勾选 README、`.gitignore` 或 License，保持空仓库。
4. 创建后复制仓库 HTTPS 或 SSH 地址。

如果使用 GitHub 免费个人版，私有仓库能运行 Actions，但 GitHub Pages 对私有仓库的可用性取决于账户套餐；Cloudflare 路线没有这个 Pages 套餐限制。

## 2. 推送本地仓库

在项目目录运行，把地址替换成你自己的：

```bash
git remote add origin https://github.com/<你的用户名>/hot-gap-aggregator.git
git push -u origin main
```

如果 HTTPS 需要认证，GitHub 密码位置要填写 Personal Access Token；也可以改用已经配置好的 SSH 地址。

## 3. 添加 GitHub Actions Secrets 和 Variable

进入仓库：`Settings` → `Secrets and variables` → `Actions`。

在 **Secrets** 标签逐个添加：

| 名称 | 是否必填 | 用途 |
| --- | --- | --- |
| `ZHIPU_API_KEY` | 必填 | 智谱翻译与摘要 |
| `YOUTUBE_API_KEY` | 选填 | YouTube 官方 API；不填则轮询 Invidious |
| `INVIDIOUS_BASE` | 选填 | 强制使用单个 Invidious 实例 |
| `BARK_URL` | 选填 | Bark 推送 |
| `FEISHU_WEBHOOK` | 选填 | 飞书自定义机器人 Webhook |
| `FEISHU_SIGN_SECRET` | 选填 | 飞书机器人开启加签后填写 |
| `RSSHUB_BASE` | 选填 | 自建 RSSHub；不填使用公共实例 |
| `TG_BOT_TOKEN` | 选填 | Telegram Bot 推送 |
| `TG_CHAT_ID` | 选填 | Telegram 目标会话 |
| `SERVERCHAN_KEY` | 选填 | Server酱推送 |
| `CLOUDFLARE_API_TOKEN` | Cloudflare 必填 | 上传 Pages 产物 |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare 必填 | Cloudflare 账户 ID |

切换到 **Variables** 标签，添加仓库变量：

- 名称：`DEPLOY_TARGET`
- 推荐值：`cloudflare`
- GitHub Pages 路线改为：`github`

变量值必须是小写的 `cloudflare` 或 `github`，否则工作流会主动停止并提示配置错误。

## 4A. Cloudflare Pages（推荐）

### 创建 Pages 项目

1. 登录 Cloudflare Dashboard。
2. 进入 `Workers & Pages` → `Create application` → `Pages`。
3. 选择 Direct Upload/直接上传方式，项目名必须填 `hot-gap-aggregator`。
4. 首次如果要求上传文件，可先完成空项目/占位部署；之后由 GitHub Actions 覆盖。

### 创建 API Token

当前 Cloudflare 控制台的可靠路径是：

1. 右上角头像 → `My Profile` → `API Tokens` → `Create Token`。
2. 如果页面提供 `Cloudflare Pages: Edit` 模板，直接使用。
3. 如果没有该模板，选择 `Create Custom Token`，权限设为：
   - Permission group：`Account`
   - Resource：`Cloudflare Pages`
   - Access：`Edit`
   - Account Resources：只包含准备部署的账户
4. 创建后立即复制 Token；Cloudflare 只展示一次。
5. 把 Token 添加为 GitHub Secret `CLOUDFLARE_API_TOKEN`。

账户 ID 可在 Cloudflare 账户首页/Overview 右侧信息区找到，也可以从页面 URL 中识别；添加为 `CLOUDFLARE_ACCOUNT_ID`。

最后确认仓库变量 `DEPLOY_TARGET=cloudflare`。

## 4B. GitHub Pages（备选）

1. 仓库 `Settings` → `Pages`。
2. `Build and deployment` → `Source` 选择 `GitHub Actions`。
3. 仓库变量设置为 `DEPLOY_TARGET=github`。

工作流会自动把 Vite base 切换为 `/hot-gap-aggregator/`；Cloudflare 路线则使用 `/`。不需要手动改代码。

## 5. 首次运行与验证

1. 打开仓库 `Actions`。
2. 左侧选择 `Collect, build and publish`。
3. 点击 `Run workflow` → `Run workflow`。
4. 等待 `build` 和所选部署步骤变绿。
5. Cloudflare：在项目 Deployments 中打开 `*.pages.dev` 地址。
6. GitHub Pages：在 Actions 部署结果或 `Settings → Pages` 中打开站点地址。
7. 检查首页更新时间、来源状态和趋势页；采集源被限流时会显示降级，不会阻断其他源或静态部署。

此后工作流会在 UTC 偶数整点运行，即北京时间每天 08:00、10:00、12:00……每两小时一次。GitHub 的定时任务可能因平台排队晚几分钟启动。

## 6. SQLite 历史维护

`data/hot.db` 是有意提交的，每两小时会产生一个新的 Git 二进制对象。建议：

- 每月在本地执行一次 `git gc`，整理本地对象；它不会删除仍在提交历史中的数据库版本。
- 每月观察仓库大小；明显变大时先备份仓库和数据库，再把当月大量 `chore: data ...` 提交压成一个月度数据基线。
- 若历史增长成为长期问题，可后续把 SQLite 移到对象存储或数据库服务；这属于架构升级，不在当前纯静态部署范围内。

不要在没有备份的情况下强推重写历史。
