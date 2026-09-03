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
并合入线上 `gongkao.json`，可在公考页用「选调生」筛选。

手动验证：

```bash
.venv/bin/python -m app.server_run --scs
.venv/bin/python -m app.server_run --subsidy
.venv/bin/python -m app.server_run --xuandiao
```

首次补贴/选调公告和政策页检查只建立基线，不发送通知。服务器日志为
`/var/log/hot-gap-server.log`。
