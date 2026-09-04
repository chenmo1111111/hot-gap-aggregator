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
cp .env.server.example .env
chmod 600 .env
cp deploy/server/hot-gap-jobs.cron /etc/cron.d/hot-gap-jobs
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

手动验证：

```bash
.venv/bin/python -m app.server_run --scs
.venv/bin/python -m app.server_run --subsidy
.venv/bin/python -m app.server_run --xuandiao
```

检查服务端数据是否完整：

```bash
python3 - <<'PY'
import json
p = json.load(open('/var/www/hot-gap/data/server-gongkao.json', encoding='utf-8'))
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
