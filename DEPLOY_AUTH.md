# 账号系统部署

账号 API 只监听百度云回环地址 `127.0.0.1:8787`，由 Nginx 在同一域名的
`/api/` 下反代。应用外壳保持公开，`/data/*.json` 必须通过签名 Cookie 鉴权。

> 重要：域名鉴权不等于数据已经私密。当前 Actions 会把 `public/data` 和
> `data/hot.db` 提交进 GitHub；如果仓库保持 Public，别人仍可从 GitHub 读取这些文件。
> 要保护聚合内容，应把仓库改为 Private，或停止把数据文件提交到仓库。

## 1. 准备管理员凭据

在服务器生成会话密钥，并自行确定首个管理员用户名和强密码。不要把输出发到聊天、
提交到 GitHub，或写入项目目录的 `.env`：

```bash
openssl rand -hex 32
```

创建仅 root 可读的 `/etc/hot-gap-sync.env`：

```ini
SESSION_SECRET=<上一步生成的随机值，至少 32 字节>
ADMIN_USER=<管理员用户名>
ADMIN_PASSWORD=<管理员强密码>
SYNC_DB_PATH=/var/lib/hot-gap-sync/sync.db
MAX_USERS=8
```

只有数据库为空时才读取 `ADMIN_USER` / `ADMIN_PASSWORD` 创建首个管理员。服务不会
把明文密码写入 SQLite，数据库中只保存 bcrypt hash。

## 2. 安装服务

```bash
cd /opt/hot-gap-aggregator
.venv/bin/pip install -r requirements-server.txt
sudo install -d -o www-data -g www-data -m 750 /var/lib/hot-gap-sync
sudo chown root:root /etc/hot-gap-sync.env
sudo chmod 600 /etc/hot-gap-sync.env
sudo cp deploy/server/hot-gap-sync.service /etc/systemd/system/hot-gap-sync.service
sudo systemctl daemon-reload
sudo systemctl enable --now hot-gap-sync
sudo systemctl status hot-gap-sync --no-pager
curl -i http://127.0.0.1:8787/api/me
```

最后一条应返回 `401`，这表示服务正常、匿名访问被拒绝。若 users 表为空但缺少
管理员环境变量，服务会记录明确错误并退出。

## 3. 切换 Nginx

先备份现有配置，再安装仓库里的完整 HTTPS 配置：

```bash
sudo cp /etc/nginx/sites-available/hot-gap /etc/nginx/sites-available/hot-gap.before-account
sudo cp deploy/server/hot-gap-nginx.conf /etc/nginx/sites-available/hot-gap
sudo sed -i '/auth_basic/d' /etc/nginx/sites-available/hot-gap
sudo nginx -t
sudo systemctl reload nginx
```

仓库配置中的 `/api/` 使用没有尾部斜杠的 `proxy_pass http://127.0.0.1:8787;`，
这样 FastAPI 才会收到完整的 `/api/login` 路径。`/data/` 同时设置 `no-store`，新版
Service Worker 也不再缓存受保护 JSON，退出后不会继续从 PWA 离线缓存读取旧数据。

## 4. 验证

```bash
curl -I https://hot.weixincuotiben.top/
curl -I https://hot.weixincuotiben.top/data/all.json
curl -i https://hot.weixincuotiben.top/api/me
```

预期：应用外壳为 `200`，匿名 `/data/all.json` 与 `/api/me` 均为 `401`。浏览器首次
打开会显示登录页；用 `ADMIN_USER` / `ADMIN_PASSWORD` 登录后，在「设置 → 用户管理」
给朋友创建账号、删除账号或重置密码。Cookie 有效期 30 天。

## 5. 回滚

若账号服务异常，先恢复 Nginx 备份，再排查服务：

```bash
sudo cp /etc/nginx/sites-available/hot-gap.before-account /etc/nginx/sites-available/hot-gap
sudo nginx -t
sudo systemctl reload nginx
sudo journalctl -u hot-gap-sync -n 100 --no-pager
```

不要删除 `/var/lib/hot-gap-sync/sync.db`；它包含用户账号和每人的同步设置。
