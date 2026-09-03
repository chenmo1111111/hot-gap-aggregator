# 在阿里云服务器部署 RSSHub

本文用于把 RSSHub 独立部署到现有阿里云 ECS，并通过现有备案域名的 `/rsshub/` 路径提供服务。RSSHub 使用 Docker 默认 `bridge` 网络，只监听 `127.0.0.1:1200`，不会加入或修改 gongkao 的 Compose 网络、PostgreSQL、数据卷或容器。

> 安全边界：不要停止、重建或修改任何 gongkao 容器；不要运行 `docker system prune` 或 `docker image prune -a`；修改 Nginx 前必须备份，只允许在目标域名的 HTTPS `server` 块中增加一个 `location /rsshub/`。

## 执行方式

按本文的检查点逐步执行。每一步完成后先核对输出，再继续下一步。向他人贴输出时必须遮住 ACCESS_KEY。

## 0. 部署前只读检查

确认生产容器仍在运行、1200 端口空闲，并记录资源基线：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
free -h
df -h /
sudo ss -lntp | grep ':1200 ' || echo 'PORT_1200_FREE'
```

通过标准：

- `gongkao-daily-accumulation-v1-app-1` 和 `gongkao-production-postgres` 均为 `Up`；
- 最后一条输出 `PORT_1200_FREE`；
- 根分区仍有足够空间（当前预计约 9.9 GB）。

如果生产容器状态异常或 1200 已被占用，立即停止部署，不要处理现有容器。

## 1. 生成 ACCESS_KEY

在服务器生成 URL 安全的十六进制密钥，并保存为仅 root 可读的文件：

```bash
umask 077
openssl rand -hex 32 > /root/.rsshub_access_key
chmod 600 /root/.rsshub_access_key
printf 'KEY_LENGTH=%s\n' "$(wc -c < /root/.rsshub_access_key | tr -d ' ')"
```

命令不会把密钥显示在终端中。需要录入 GitHub Secret 时，才在服务器本地运行 `cat /root/.rsshub_access_key` 并直接复制到 GitHub 表单。不要把它提交到 Git、写入 `config/feeds.yaml`，也不要在聊天或截图中公开。

检查点：`KEY_LENGTH=65`，其中 64 个字符是密钥，另 1 个是文件末尾换行。贴回结果时只回复长度，不要贴密钥本身。

## 2. 启动 RSSHub 容器

把密钥安全地读入当前 shell，不在终端回显：

```bash
RSSHUB_ACCESS_KEY="$(tr -d '\r\n' < /root/.rsshub_access_key)"
printf 'LOADED_KEY_LENGTH=%s\n' "${#RSSHUB_ACCESS_KEY}"
```

然后启动独立容器：

```bash
docker run -d --name rsshub --restart unless-stopped \
  -p 127.0.0.1:1200:1200 --memory=350m --memory-swap=350m \
  -e NODE_ENV=production -e CACHE_EXPIRE=7200 -e ACCESS_KEY="$RSSHUB_ACCESS_KEY" \
  ghcr.io/diygod/rsshub:latest
```

等待约 20 秒后检查：

```bash
docker ps --filter name=rsshub
docker logs --tail 20 rsshub
docker inspect rsshub --format 'OOMKilled={{.State.OOMKilled}} Restarts={{.RestartCount}}'
docker stats rsshub --no-stream
```

通过标准：

- 容器状态为 `Up`；
- `OOMKilled=false`、`Restarts=0`；
- 日志中没有持续重复的启动错误。

如果 `OOMKilled=true` 或容器反复重启，只删除 RSSHub 自己并提高限额，不能操作其他容器：

```bash
docker rm -f rsshub
docker run -d --name rsshub --restart unless-stopped \
  -p 127.0.0.1:1200:1200 --memory=450m --memory-swap=700m \
  -e NODE_ENV=production -e CACHE_EXPIRE=7200 -e ACCESS_KEY="$RSSHUB_ACCESS_KEY" \
  ghcr.io/diygod/rsshub:latest
```

再次运行上面的四条检查命令。若仍反复重启，停止部署并保留日志，不要继续增加内存或干预 gongkao。

## 3. 在服务器本机验证 RSSHub

仍在保存了 `RSSHUB_ACCESS_KEY` 的同一个 shell 中执行。响应正文不能直接贴出，因为 RSS/Atom 的 `self` 链接可能回显 KEY：

```bash
HTTP_STATUS="$(curl -sS --get \
  --data-urlencode "key=${RSSHUB_ACCESS_KEY}" \
  -o /tmp/rsshub-test.xml -w '%{http_code}' \
  'http://127.0.0.1:1200/zhihu/hot')"
printf 'HTTP_STATUS=%s\n' "$HTTP_STATUS"
if grep -Eq '<rss([ >])|<feed([ >])|<item([ >])|<entry([ >])' /tmp/rsshub-test.xml; then
  echo 'RSS_XML_OK'
else
  echo 'RSS_XML_NOT_FOUND'
  sed -E 's/(key=)[^&"<]+/\1REDACTED/g' /tmp/rsshub-test.xml | head -c 400
  echo
fi
```

通过标准：响应为 XML，并出现 `<rss`、`<feed`、`<item>` 或 `<entry>`。

- `401`/`403`：检查传入的 key 是否与启动容器时完全一致；只比较值，不打印密钥：`CONTAINER_KEY="$(docker inspect rsshub --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^ACCESS_KEY=//p')"; [ "$RSSHUB_ACCESS_KEY" = "$CONTAINER_KEY" ] && echo KEY_MATCH || echo KEY_MISMATCH; unset CONTAINER_KEY`。
- 空响应或错误页：运行 `docker logs --tail 100 rsshub` 后停止，先排查日志。
- 单个上游路由报错不代表 RSSHub 容器失效；可再访问根页确认服务进程存在：`curl -I http://127.0.0.1:1200/`。

## 4. 接入现有备案域名的 Nginx

### 4.1 先确定域名和配置文件

不要猜文件。执行：

```bash
sudo nginx -T 2>/dev/null | grep -nE '(^# configuration file|server_name|listen .*443)'
```

从输出中找到 gongkao 备案域名，以及包含其 `listen 443 ssl` 和 `server_name` 的配置文件。常见位置是 `/etc/nginx/sites-enabled/` 或 `/etc/nginx/conf.d/`。

在继续前明确记录：

```text
域名：<GONGKAO_DOMAIN>
实际配置文件：<NGINX_CONFIG_FILE>
```

如果 `sites-enabled` 中是符号链接，应编辑它指向的 `sites-available` 原文件，可用以下命令确认：

```bash
readlink -f <NGINX_CONFIG_FILE>
```

### 4.2 必须先备份

将下面路径替换成上一步得到的真实原文件路径：

```bash
NGINX_SITE='<真实的 nginx 配置文件路径>'
test -f "$NGINX_SITE" || { echo 'NGINX_FILE_NOT_FOUND'; exit 1; }
NGINX_BACKUP="${NGINX_SITE}.bak.$(date +%s)"
sudo cp -- "$NGINX_SITE" "$NGINX_BACKUP"
sudo stat -c '%U:%G %a %n' "$NGINX_SITE" "$NGINX_BACKUP"
echo "BACKUP=$NGINX_BACKUP"
```

必须看到原文件和 `.bak.<时间戳>` 备份都存在，才能编辑。

### 4.3 只在目标域名的 443 server 块中增加 location

打开真实配置文件：

```bash
sudo nano "$NGINX_SITE"
```

只在该域名包含 `listen 443 ssl` 的 `server { ... }` 内加入下面这一段，不修改其他内容：

```nginx
    location /rsshub/ {
        proxy_pass http://127.0.0.1:1200/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }
```

`proxy_pass` 末尾的 `/` 不能删。它负责去掉外部请求的 `/rsshub/` 前缀：

```text
https://域名/rsshub/zhihu/hot
                 ↓
http://127.0.0.1:1200/zhihu/hot
```

保存后先测试，只有同时出现 `syntax is ok` 和 `test is successful` 才能 reload：

```bash
if sudo nginx -t; then
  sudo systemctl reload nginx
  echo 'NGINX_RELOAD_OK'
else
  echo "NGINX_TEST_FAILED; restoring $NGINX_BACKUP"
  sudo cp -- "$NGINX_BACKUP" "$NGINX_SITE"
  sudo nginx -t && sudo systemctl reload nginx
  echo 'NGINX_RESTORED'
fi
```

若输出 `NGINX_RESTORED`，说明新增配置未生效，停止并检查编辑位置，不能反复盲改。

### 4.4 外网验证

替换为真实备案域名，仍使用当前 shell 中的 key。只输出状态和 XML 判定，不输出可能包含 KEY 的正文：

```bash
GONGKAO_DOMAIN='<备案域名，不含 https://>'
HTTP_STATUS="$(curl -sS --get \
  --data-urlencode "key=${RSSHUB_ACCESS_KEY}" \
  -o /tmp/rsshub-public-test.xml -w '%{http_code}' \
  "https://${GONGKAO_DOMAIN}/rsshub/zhihu/hot")"
printf 'HTTP_STATUS=%s\n' "$HTTP_STATUS"
if grep -Eq '<rss([ >])|<feed([ >])|<item([ >])|<entry([ >])' /tmp/rsshub-public-test.xml; then
  echo 'RSS_XML_OK'
else
  echo 'RSS_XML_NOT_FOUND'
  sed -E 's/(key=)[^&"<]+/\1REDACTED/g' /tmp/rsshub-public-test.xml | head -c 400
  echo
fi
```

通过标准：外网 HTTPS 响应也是 XML，并出现 `<rss`、`<feed`、`<item>` 或 `<entry>`。如果本机 127.0.0.1 成功、外网失败，先检查 Nginx 配置位置和日志，不要开放公网 1200 端口：

```bash
sudo tail -n 50 /var/log/nginx/error.log
```

## 5. 配置 GitHub Actions Secrets

进入 GitHub 仓库：`Settings → Secrets and variables → Actions → Secrets → New repository secret`，新增或更新：

| Secret | 值 |
| --- | --- |
| `RSSHUB_BASE` | `https://<备案域名>/rsshub` |
| `RSSHUB_KEY` | 第 1 步的完整 `<KEY>` |

`RSSHUB_BASE` 末尾不要加 `/`。不要把这两个值放进 Repository variables、源码或 Actions 日志。

## 6. 验证 hot-gap-aggregator

1. 打开 GitHub 仓库的 `Actions`。
2. 选择 `Collect, build and publish`。
3. 点击 `Run workflow`，选择 `main` 并确认。
4. 运行结束后打开摘要，检查是否出现 `feed ... failed`；记录失败的具体路由。
5. 登录部署站点，确认「AI动态」「工具更新」有卡片，并检查「顶刊」中是否出现 RSSHub 论文条目。
6. 也可在已登录网站中确认 `data/ai.json`、`data/tools.json` 有内容。

单个路由失败不会影响其他数据源。对于持续失败的路由，在 `config/feeds.yaml` 中注释掉，或从 <https://docs.rsshub.app> 查找当前替代路由，然后再 commit、push。

### X-MOL 占位符

当前 `config/feeds.yaml` 中：

```yaml
- {name: "X-MOL 生物信息学", route: "/x-mol/paper/0/<待填magazine_id>", tab: papers, translate: false, limit: 15}
```

`<待填magazine_id>` 不是可用值。打开 x-mol.com 对应期刊页，从页面 URL 中查找数字期刊 ID，并用它替换占位符。若 URL 中无法确认 ID，就删除或继续注释这一行，不能把占位符投入生产。

## 日常运维

### 查看资源和健康状态

```bash
docker ps --filter name=rsshub
docker stats rsshub --no-stream
docker inspect rsshub --format 'OOMKilled={{.State.OOMKilled}} Restarts={{.RestartCount}}'
free -h
df -h /
docker logs --tail 50 rsshub
```

RSSHub 镜像及其层可能占用约 1.5 GB，应定期关注 `df -h /`。不要为了腾空间运行全局 prune，以免影响 gongkao 使用的镜像。

### 更新 RSSHub

确认 `/root/.rsshub_access_key` 仍存在，然后只更新 RSSHub 自己：

```bash
docker pull ghcr.io/diygod/rsshub:latest
RSSHUB_ACCESS_KEY="$(tr -d '\r\n' < /root/.rsshub_access_key)"
docker rm -f rsshub
docker run -d --name rsshub --restart unless-stopped \
  -p 127.0.0.1:1200:1200 --memory=350m --memory-swap=350m \
  -e NODE_ENV=production -e CACHE_EXPIRE=7200 -e ACCESS_KEY="$RSSHUB_ACCESS_KEY" \
  ghcr.io/diygod/rsshub:latest
```

更新后重新执行步骤 2 的健康检查和步骤 3、4.4 的内外网验证。如果历史上发生过 OOM，应沿用 `--memory=450m --memory-swap=700m` 的限额。

## 如何关闭 RSSHub

只有明确决定停用时才执行以下操作。

### 1. 只停止并删除 RSSHub 容器

```bash
docker stop rsshub
docker rm rsshub
```

不要删除 gongkao 容器、网络、卷或 PostgreSQL。

### 2. 删除 Nginx 中的 RSSHub location

先再次备份当前配置：

```bash
NGINX_SITE='<真实的 nginx 配置文件路径>'
NGINX_BACKUP="${NGINX_SITE}.bak.$(date +%s)"
sudo cp -- "$NGINX_SITE" "$NGINX_BACKUP"
echo "BACKUP=$NGINX_BACKUP"
sudo nano "$NGINX_SITE"
```

只删除此前增加的 `location /rsshub/ { ... }` 整段，然后执行：

```bash
if sudo nginx -t; then
  sudo systemctl reload nginx
else
  sudo cp -- "$NGINX_BACKUP" "$NGINX_SITE"
  sudo nginx -t && sudo systemctl reload nginx
fi
```

### 3. 删除聚合端 Secret

在 GitHub `Settings → Secrets and variables → Actions` 中删除 `RSSHUB_BASE`；也可以一并删除 `RSSHUB_KEY`。采集器检测不到 `RSSHUB_BASE` 后会跳过 feed 源，其他来源和网站部署不受影响。

## 故障边界

- RSSHub 容器失败：只检查或重建 `rsshub`，不操作 gongkao。
- 某个 RSSHub 路由失败：禁用该条 feed，不重启生产项目。
- Nginx 测试失败：立即从本次 `.bak.<时间戳>` 恢复，不 reload 错误配置。
- 内存不足：先停止 RSSHub；不要通过停止 PostgreSQL 或 gongkao 为 RSSHub 腾内存。
