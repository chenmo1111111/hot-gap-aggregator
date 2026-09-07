# 高校就业网国内服务器任务

高校就业网只在国内服务器运行，输出独立的 `server-jobs.json`，不会覆盖 GitHub
Actions 生成的岗位数据，也不会修改同机其它应用或容器。

## 更新代码与配置

将本仓库更新到服务器 `/opt/hot-gap-aggregator` 后，在该目录执行：

```bash
cd /opt/hot-gap-aggregator
.venv/bin/pip install -r requirements-server.txt
cp .env.server.example .env.example.reference
```

确认服务器原有 `.env` 中存在以下两项；真实密钥继续保留在 `.env`，不要提交：

```dotenv
CAMPUS_JOBS_CONFIG=config/campus_jobs_sources.yaml
SERVER_SITE_DATA_DIR=/var/www/hot-gap/data
```

## 手动验证

```bash
cd /opt/hot-gap-aggregator
.venv/bin/python -m app.server_run --campus-jobs
```

成功后检查 sidecar：

```bash
.venv/bin/python -c "import json; p=json.load(open('/var/www/hot-gap/data/server-jobs.json',encoding='utf-8')); print(p['status']); print(p.get('subsources'))"
```

首次运行建立去重基线，不推送旧公告。以后新招聘会进入岗位页；“选调/定向”公告
同时进入公考页和预警链。

## 每 12 小时运行

仓库提供 `deploy/server/hot-gap-jobs.cron`。若服务器已安装该文件，只增加下面一行
即可，不需要重启任何 Docker 容器：

```cron
55 */12 * * * root cd /opt/hot-gap-aggregator && /opt/hot-gap-aggregator/.venv/bin/python -m app.server_run --campus-jobs >> /var/log/hot-gap-server.log 2>&1
```

写入后验证：

```bash
sudo chmod 644 /etc/cron.d/hot-gap-jobs
sudo systemctl reload cron
sudo grep campus-jobs /etc/cron.d/hot-gap-jobs
```

单站失败只记录 `degraded`，不会中断公考、小红书规则监控、网站发布或同机业务。
