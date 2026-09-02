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

在 `.env` 中填写真实的城市 URL、智谱 Key、Bark 或飞书 Webhook 后，可手动验证：

```bash
.venv/bin/python -m app.server_run --scs
.venv/bin/python -m app.server_run --city
```

首次补贴检查只建立基线，不发送通知。服务器日志为
`/var/log/hot-gap-server.log`。
