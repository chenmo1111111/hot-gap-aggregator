# 百度云服务器任务

`scs.gov.cn` 旧首页已于 2026 年 8 月下线；脚本改从同一官方专题站的
`dl.scs.gov.cn` 公开接口读取招考公告，并动态解析当前考试 ID。

服务器目录：`/opt/hot-gap-aggregator`。服务器任务兼容 Ubuntu 自带的 Python 3.10+。
建议使用 root 安装、运行国内站任务，
网站仍由 `deploy` 用户和 GitHub Actions 发布到 `/var/www/hot-gap`。

```bash
cd /opt/hot-gap-aggregator
python3 -m venv .venv
.venv/bin/pip install -r requirements-server.txt
.venv/bin/playwright install --with-deps chromium
cp .env.server.example .env
chmod 600 .env
cp deploy/server/hot-gap-jobs.cron /etc/cron.d/hot-gap-jobs
# 防止从 Windows 手工复制时 CRLF 让 cron 命令尾部出现 ^M。
sed -i 's/\r$//' /etc/cron.d/hot-gap-jobs
chmod 644 /etc/cron.d/hot-gap-jobs
```

`config/subsidy_sources.yaml` 已包含目标地区的官方公告栏目和 3 个核心政策页。
如需政策正文变化由模型判断，在 `.env` 填 `ZHIPU_API_KEY` 或 `DEEPSEEK_API_KEY`。
推送优先用飞书，其次 Bark；两者都不填则写入网站的 `data/alerts.json`。

`config/xuandiao_sources.yaml` 监听黑龙江、辽宁、河北、天津、山东五省官方页面，
每 6 小时运行；命中东北林业大学/东北林大/NEFU 的公告会最高优先级单独推送，
并写入服务器独占的 `data/server-gongkao.json`，可在公考页用「选调生」筛选。
前端在登录后把该文件与 GitHub 生成的粉笔数据合并；发布工作流永久排除
`data/server-*.json`，因此后续部署不会再覆盖国家公务员局和各省选调公告。

`config/campus_jobs_sources.yaml` 默认监听东北林业大学就业信息网的招聘公告，
每 12 小时运行。普通招聘写入服务器独占的 `data/server-jobs.json`；标题含
“选调/定向”的公告还会同时写入 `data/server-gongkao.json` 并走现有飞书/Bark/
站内预警链。两个 sidecar 都不会改写 GitHub Actions 生成的 `jobs.json`、
`gongkao.json` 或 `all.json`。配置文件里可以继续追加目标高校。

`config/yingjiesheng.yaml` 的应届生求职网也由国内服务器运行。服务器 `.env` 必须有：

```dotenv
YINGJIESHENG_ON_SERVER=true
YINGJIESHENG_CONFIG=config/yingjiesheng.yaml
HAITOU_CONFIG=config/haitou.yaml
WUTONGGUO_CONFIG=config/wutongguo.yaml
RETENTION_CONFIG=config/retention.yaml
SERVER_HEARTBEAT_PATH=/var/www/hot-gap/data/server-heartbeat.txt
```

两小时主任务同时执行 `--scs --yingjiesheng`；后一个开关会并行运行应届生、海投网
和梧桐果，任一站失败都保留其它站结果。三个站都失败时不会清空上一版非过期
`server-jobs.json`。海投/梧桐果单次请求 8 秒内快速降级。GitHub Actions 设置
`YINGJIESHENG_ON_SERVER=true` 后会直接跳过这些国内站，仍能快速完成其它来源。

主任务完成后原子更新 `server-heartbeat.txt`。独立小时检查发现时间戳超过 4 小时
时，只发送一次“公考聚合主采集停了,GitHub Actions 每日兜底仍在”；下一次正常
心跳会形成新的监控周期。推送复用飞书/Bark，不配置渠道时只记录检查日志。

手动验证：

```bash
.venv/bin/python -m app.server_run --scs
.venv/bin/python -m app.server_run --subsidy
.venv/bin/python -m app.server_run --xuandiao
.venv/bin/python -m app.server_run --xhs-rules
.venv/bin/python -m app.server_run --campus-jobs
.venv/bin/python -m app.server_run --scs --yingjiesheng
.venv/bin/python -m app.check_server_heartbeat --max-age-hours 4
```

## 小红书电商规则监控

监控使用桌面 Chromium 登录小红书学习中心，每 8 小时检查“规则修订、规则新增、
意见征集”和配置中的重点规则正文。Cookie 只放服务器
`/opt/hot-gap-aggregator/.env`：

```dotenv
XHS_RULE_WATCH_CONFIG=config/xhs_rule_watch.yaml
XHS_SCHOOL_COOKIE_JSON='[{"name":"web_session","value":"...","domain":".xiaohongshu.com","path":"/"}]'
XHS_SCHOOL_COOKIE_DOC='a1=...; webId=...; gid=...'
```

第一项粘贴 Cookie-Editor 导出的完整 JSON 数组，第二项粘贴页面 Console 里的
`document.cookie`。真实值不得提交到 Git。登录失效后会通过飞书、Bark 或站内
`alerts.json` 提醒重新导出。正文首次运行只建立基线；以后公示/生效/修订日期、
腾讯文档链接或正文变化才交给模型判断。安装或升级后把 cron 模板复制到系统：

```bash
cp deploy/server/hot-gap-jobs.cron /etc/cron.d/hot-gap-jobs
sed -i 's/\r$//' /etc/cron.d/hot-gap-jobs
chmod 644 /etc/cron.d/hot-gap-jobs
systemctl restart cron
```

Playwright Chromium 峰值约 500MB。运行前用 `free -h` 检查 available 内存并确保
服务器已有 swap；任务结束后浏览器会关闭，不会常驻占用。

检查服务端数据是否完整：

```bash
python3 - <<'PY'
import json
p = json.load(open('/var/www/hot-gap/data/server-gongkao.json', encoding='utf-8'))
print(p['generated_at'], p['status'])
print({key: value['item_count'] for key, value in p.get('subsources', {}).items()})
PY

python3 - <<'PY'
import json
p = json.load(open('/var/www/hot-gap/data/server-jobs.json', encoding='utf-8'))
print(p['generated_at'], p['status'])
print({key: value['item_count'] for key, value in p.get('subsources', {}).items()})
PY
```

首次补贴/选调公告和政策页检查只建立基线，不发送通知。服务器日志为
`/var/log/hot-gap-server.log`。

## GitHub Actions 定时触发

`hotgap-github-trigger`、对应的 service 和 timer 会每 3 小时请求 GitHub
运行一次 `publish.yml`。采集与构建仍在 GitHub 执行，百度云只发送一个很小的
HTTPS 请求，不占用小程序的计算资源。GitHub 工作流自身每天保留一次定时运行，
用作令牌失效或百度云临时不可用时的兜底。

真实令牌不能提交到仓库。把 `github-trigger.env.example` 复制到服务器的
`/etc/hot-gap/github-trigger.env`，权限设为 `0600`；令牌使用仅限本仓库、
具有 `Actions: Read and write` 权限的 fine-grained token。
