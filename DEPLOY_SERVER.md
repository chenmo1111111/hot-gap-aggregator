# 百度云服务器部署 Runbook

目标：把 `hot-gap-aggregator` 的纯静态 `site/` 部署到百度云 Ubuntu 服务器，通过 `hot.weixincuotiben.top` 访问。

工作方式：GitHub Actions 每两小时采集和构建，然后用低权限 `deploy` 用户通过 SSH + `rsync` 同步到 `/var/www/hot-gap`。服务器只由现有 Nginx 提供静态文件，不运行 Python 或 Node 服务。

> 请严格按 A → G 的顺序执行。每完成一节，先核对该节的“成功标志”，再进入下一节。不要修改或删除服务器已有的 Nginx 站点和服务。

## A. Windows：生成部署专用 SSH 密钥

这一步会在当前 Windows 用户的 `.ssh` 文件夹中创建一对只供 GitHub Actions 部署使用的密钥，不会改变现有登录密钥。

先打开 PowerShell，检查目标文件是否已经存在：

```powershell
Test-Path "$env:USERPROFILE\.ssh\hotgap_deploy"
```

如果结果是 `False`，执行：

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\hotgap_deploy" -N '""' -C "github-actions-hotgap"
```

如果结果是 `True`，不要覆盖，先确认这是不是之前为本项目生成的部署密钥。

查看公钥（稍后放到服务器）：

```powershell
type $env:USERPROFILE\.ssh\hotgap_deploy.pub
```

查看私钥（稍后放到 GitHub Secret，不能发给其他人）：

```powershell
type $env:USERPROFILE\.ssh\hotgap_deploy
```

复制私钥时必须包含 `-----BEGIN OPENSSH PRIVATE KEY-----` 和 `-----END OPENSSH PRIVATE KEY-----` 两行。

成功标志：目录中存在 `hotgap_deploy` 和 `hotgap_deploy.pub` 两个文件，公钥以 `ssh-ed25519` 开头。

## B. Ubuntu：创建低权限用户和独立站点目录

这一步会新增用户 `deploy` 和目录 `/var/www/hot-gap`，不会修改已有应用目录或已有 Nginx 站点。

先使用你现有的管理员账号 SSH 登录服务器：

```powershell
ssh <现有管理员用户名>@120.48.78.40
```

登录后逐行执行：

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo mkdir -p /var/www/hot-gap
sudo chown -R deploy:www-data /var/www/hot-gap
sudo chmod 755 /var/www/hot-gap
sudo -u deploy mkdir -p /home/deploy/.ssh
echo '<粘贴 A 步骤的完整公钥>' | sudo tee -a /home/deploy/.ssh/authorized_keys
sudo chown -R deploy:deploy /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh && sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

如果提示用户 `deploy` 已存在，跳过第一行，继续检查目录和权限。不要重复追加同一把公钥；可先执行：

```bash
sudo cat /home/deploy/.ssh/authorized_keys
```

回到 Windows PowerShell，测试密钥和目录权限：

```powershell
ssh -i "$env:USERPROFILE\.ssh\hotgap_deploy" deploy@120.48.78.40 "id && test -w /var/www/hot-gap && echo WRITE_OK"
```

成功标志：输出包含 `uid=... deploy` 和 `WRITE_OK`。

## B1. Nginx 配置：命令行版与宝塔版二选一

### 方案一：原生 Nginx 命令行

这一步只新建 `/etc/nginx/sites-available/hot-gap`，不会改动已有站点文件。

在 Ubuntu 上执行：

```bash
sudo tee /etc/nginx/sites-available/hot-gap >/dev/null <<'NGINX'
server {
    listen 80;
    server_name hot.weixincuotiben.top;
    root /var/www/hot-gap;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
    location /data/ { add_header Cache-Control "public, max-age=300"; }
    gzip on;
    gzip_types application/json application/javascript text/css image/svg+xml;
}
NGINX

sudo ln -s /etc/nginx/sites-available/hot-gap /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

如果软链接已存在，`ln` 会提示 `File exists`，不用重复创建；单独运行下面一行即可：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

成功标志：`nginx -t` 输出 `syntax is ok` 和 `test is successful`。

### 方案二：宝塔面板

如果服务器的 Nginx 由宝塔管理，不执行上面的命令行版：

1. 进入宝塔面板 → `网站` → `添加站点`。
2. 域名填写 `hot.weixincuotiben.top`。
3. 根目录填写 `/var/www/hot-gap`。
4. PHP 版本选择 `纯静态`。
5. 站点设置 → `伪静态`，填写：

```nginx
try_files $uri $uri/ /index.html;
```

6. 保存后确认没有修改其他站点的域名、根目录或反向代理配置。

成功标志：宝塔网站列表出现独立的 `hot.weixincuotiben.top` 站点，根目录为 `/var/www/hot-gap`。

## C. 百度云安全组与服务器防火墙

这一步只开放网站需要的入站端口，不改现有应用端口。

1. 登录百度智能云控制台。
2. 找到云服务器实例 `120.48.78.40`。
3. 进入实例 → `安全组`（部分界面显示为 `防火墙`）→ `添加规则`。
4. 新增 TCP 入站端口 `80`，来源 `0.0.0.0/0`。
5. 新增 TCP 入站端口 `443`，来源 `0.0.0.0/0`。
6. SSH 端口通常已经开放；不要关闭当前使用的 SSH 端口。

如果 Ubuntu 启用了 UFW，再执行：

```bash
sudo ufw status
sudo ufw allow 'Nginx Full'
```

成功标志：安全组存在 80/443 入站规则；`sudo ufw status` 为 inactive，或规则中包含 `Nginx Full ALLOW`。

## D. DNS：确认服务商并添加子域名

这一步会把 `hot.weixincuotiben.top` 指向百度云服务器。

先在 Windows PowerShell 查询主域名使用哪家的 DNS：

```powershell
nslookup -type=ns weixincuotiben.top
```

- 如果结果类似 `*.alidns.com`，去阿里云 DNS 控制台添加记录。
- 如果结果是百度云 DNS 名称，去百度智能云 DNS 控制台添加记录。

添加一条记录：

| 项目 | 值 |
| --- | --- |
| 记录类型 | `A` |
| 主机记录 | `hot` |
| 记录值 | `120.48.78.40` |
| TTL | 默认 |

等待 1–10 分钟后检查：

```powershell
nslookup hot.weixincuotiben.top
```

成功标志：查询结果包含 `120.48.78.40`。DNS 没生效前不要申请 HTTPS 证书。

## E. GitHub：配置部署 Secrets 和切换目标

这一步把服务器地址和部署私钥安全交给 GitHub Actions。私钥只放 Secret，不提交到仓库。

进入仓库：`Settings` → `Secrets and variables` → `Actions` → `Secrets`，添加：

| Secret | 值 |
| --- | --- |
| `DEPLOY_SSH_HOST` | `120.48.78.40` |
| `DEPLOY_SSH_USER` | `deploy` |
| `DEPLOY_SSH_PATH` | `/var/www/hot-gap` |
| `DEPLOY_SSH_KEY` | A 步骤的私钥全文，包括 BEGIN/END 行 |
| `DEPLOY_SSH_PORT` | SSH 不是 22 时才填写；否则可留空 |

然后切换到 `Variables` 标签，把仓库变量 `DEPLOY_TARGET` 从 `github` 改为：

```text
server
```

成功标志：Actions Secrets 列表出现四个必填名称，Variable 中显示 `DEPLOY_TARGET=server`。

## F. 首次上线与 HTTPS

### F1. 运行部署

这一步会执行一次采集、构建，并用 `rsync --delete` 把 `site/` 同步到专用目录 `/var/www/hot-gap`。它不会访问其他目录。

1. GitHub 仓库 → `Actions`。
2. 选择 `Collect, build and publish`。
3. 点击 `Run workflow` → `Run workflow`。
4. 等待 `Deploy to server over SSH` 变绿。

先打开：

```text
http://hot.weixincuotiben.top
```

成功标志：能看到信息差日报首页，数据请求正常，刷新页面不会 404。

### F2. 申请 HTTPS：命令行版

确认 HTTP 与 DNS 都正常后，在 Ubuntu 执行。把邮箱占位符替换为你的真实邮箱：

```bash
sudo apt update && sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d hot.weixincuotiben.top --agree-tos -m <用户邮箱> --redirect
```

检查自动续期：

```bash
sudo certbot renew --dry-run
```

### F3. 申请 HTTPS：宝塔版

宝塔面板 → 网站 → `hot.weixincuotiben.top` → 设置 → `SSL` → `Let's Encrypt` → 选择域名 → 申请，并开启“强制 HTTPS”。

最终成功标志：打开 `https://hot.weixincuotiben.top` 正常，访问 HTTP 会自动跳转 HTTPS。

## G. SSH 安全收尾

服务器此前有大量 SSH 扫描记录。本步骤会关闭密码登录，只保留密钥登录。**deploy 用户没有 sudo 权限，所以仅验证 deploy 密钥还不够；必须先确认现有管理员账号也能通过密钥在第二个窗口登录，并保持当前管理员 SSH 会话不要关闭。**

先从另一个 Windows PowerShell 窗口测试现有管理员的密钥登录：

```powershell
ssh -i "<管理员私钥路径>" <现有管理员用户名>@120.48.78.40 "sudo -n true || echo ADMIN_KEY_LOGIN_OK"
```

如果管理员目前只能使用密码登录，暂停本节：先给管理员账号配置一把单独的 SSH 公钥并验证新窗口登录成功。不要因为 deploy 用户可以登录就关闭管理员密码登录。

先检查所有可能生效的配置：

```bash
sudo grep -RniE '^\s*PasswordAuthentication' /etc/ssh/sshd_config /etc/ssh/sshd_config.d 2>/dev/null
```

Ubuntu 推荐新建独立配置片段，避免破坏原文件：

```bash
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/99-disable-password.conf
sudo sshd -t && sudo systemctl restart ssh
sudo sshd -T | grep '^passwordauthentication'
```

不要关闭当前会话。另开两个 Windows PowerShell，分别再次确认管理员密钥与部署密钥登录；下面是部署密钥检查：

```powershell
ssh -i "$env:USERPROFILE\.ssh\hotgap_deploy" deploy@120.48.78.40 "id"
```

确认 `deploy` 没有 sudo 权限且只能写站点目录：

```bash
sudo -l -U deploy
sudo -u deploy test -w /var/www/hot-gap && echo SITE_WRITE_OK
sudo -u deploy test ! -w /etc/nginx && echo NGINX_PROTECTED
```

成功标志：新会话能用密钥登录；`deploy` 没有 sudo 权限；输出 `SITE_WRITE_OK` 和 `NGINX_PROTECTED`。

## 切换后的行为

- `DEPLOY_TARGET=server` 后，GitHub Pages 部署 job 自动跳过，旧 GitHub Pages 版本会停留在最后一次构建，不影响新站。
- 可以忽略旧站，也可以进入 GitHub 仓库 `Settings` → `Pages` 关闭。
- 前端数据请求均为 `./data/xxx.json` 相对路径，子域名根目录和 Vite base `/` 正好匹配，无需改前端。
- `rsync --delete` 只针对专用目录 `/var/www/hot-gap`；不要把 `DEPLOY_SSH_PATH` 设置为 `/var/www` 或服务器根目录。
