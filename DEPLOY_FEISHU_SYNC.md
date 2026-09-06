# S1：公考 + 秋招同步到飞书多维表格

同步任务只部署在 S1，读取 `/var/www/hot-gap/data/gongkao.json` 和
`/var/www/hot-gap/data/qiuzhao.json`。默认情况下，秋招文件由 S1 上现有的 `jobs.json`
标准化生成；如果同目录存在人工抓取的标准化快照 `qiuzhao_wanqing.json`，导出器会优先使用
该快照，避免定时任务把抓取结果覆盖回 `jobs.json`。不要把同步任务加入 GitHub Actions：
飞书 API 从美国 IP 访问不稳定。

## 1. 新建多维表格和数据表

在飞书中新建一个属于自己的多维表格，在其中建立两张数据表，名称分别为 `公考`、`秋招`。
新表自带的第一个主字段可以直接重命名为 `同步ID`。字段名称和类型必须与下表完全一致；
尤其不要在字段名首尾增加空格，否则 API 写入会报“字段不存在”。

### 公考表字段

| 字段 | 飞书字段类型 | 选项或说明 |
| --- | --- | --- |
| 同步ID | 文本 | 脚本使用，建议隐藏，用户不填 |
| 更新时间 | 日期 | 可显示到秒 |
| 地区 | 文本 | 例如 `山东·德州` |
| 招录类型 | 单选 | 国考 / 省考 / 事业单位 / 选调生 / 教师 / 医疗 / 三支一扶 / 公安 / 军队文职 / 国企 / 银行 / 其他 |
| 招录单位·公告 | 文本 |  |
| 招录人数 | 文本 |  |
| 报名开始 | 日期 |  |
| 报名截止 | 日期 |  |
| 距截止天数 | 数字 | 普通数字字段，不要设成公式 |
| 笔试时间 | 日期 |  |
| 报名状态 | 单选 | 未开始 / 报名中 / 已截止 / 待笔试 |
| 公告链接 | 超链接 |  |
| 应届可报 | 复选框 |  |
| 来源 | 单选 | 自动 / 手动；建议隐藏 |
| 备注 | 文本 |  |

### 秋招表字段

| 字段 | 飞书字段类型 | 选项或说明 |
| --- | --- | --- |
| 同步ID | 文本 | 脚本使用，可隐藏，用户不填 |
| 更新时间 | 日期 | 可显示到秒 |
| 公司名称 | 文本 |  |
| 企业性质 | 单选 | 央企 / 国企 / 民企 / 外企 / 银行 / 事业单位 / 其他 |
| 行业 | 文本 |  |
| 招聘岗位 | 文本 |  |
| 工作地点 | 文本 |  |
| 学历要求 | 文本 |  |
| 届次 | 文本 |  |
| 网申截止 | 日期 |  |
| 距截止天数 | 数字 | 普通数字字段，不要设成公式 |
| 是否笔试 | 复选框 |  |
| 投递链接 | 超链接 |  |
| 公告链接 | 超链接 |  |
| 来源 | 单选 | 自动 / 手动 |
| 备注 | 文本 |  |

## 2. 建立飞书自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，登录当前多维表格所属的飞书组织。
2. 选择“创建企业自建应用”，填写名称，例如“Hot Gap 同步”。
3. 在应用的“凭证与基础信息”中取得 App ID 和 App Secret。App Secret 只放到 S1 的
   未跟踪 `.env`，不要发到聊天、提交到仓库或写入配置文件。
4. 在“权限管理”中搜索并开通 `bitable:app`（查看、评论、编辑和管理多维表格）。
5. 如果当前组织要求应用发布或管理员审批，创建应用版本并发布，等管理员批准权限后再继续。

仅开 API 权限还不能访问这份表格，下一步的“文档应用协作者”也必须完成。

## 3. 把应用加为可编辑协作者

1. 回到刚建的多维表格。
2. 打开右上角或表格标题附近的“⋯ 更多”，选择“添加文档应用”。
3. 搜索刚才的自建应用并添加，将权限设为“可编辑”。

若找不到应用，先检查：应用和多维表格是否属于同一个飞书组织、应用版本是否已发布、
管理员是否已批准权限。普通“分享给成员”不能代替“添加文档应用”。

## 4. 取得 app_token 和 table_id

在浏览器中打开多维表格，地址通常类似：

```text
https://example.feishu.cn/base/bascnXXXXXXXXXXXX?table=tblXXXXXXXXXXXX&view=vewXXXXXXXX
```

- `/base/` 后、下一个 `?` 前的 `bascn...` 是 `app_token`。
- 分别打开“公考”和“秋招”表；URL 中 `?table=` 后的 `tbl...` 是各自的 `table_id`。
- `view=vew...` 不是 table_id，不要复制错。

在项目的 `config/feishu_sync.yaml` 中填写这三个值：

```yaml
app_token: "bascn..."
gongkao_table_id: "tbl..."
qiuzhao_table_id: "tbl..."
```

配置中 `field_mapping` 的右侧就是飞书字段名。默认已经与本文字段清单一致；若修改右侧，
必须同时把飞书字段改成完全相同的名称。`|` 左侧表示按顺序尝试多个源 JSON 字段，`$` 开头
的字段由脚本生成，不要修改其左侧名称。

## 5. 建立预置视图

这些视图在飞书中手动创建，脚本不会创建、删除或修改视图。筛选“距截止天数”时同时加上
“不为空”条件，避免空截止日期被包含。

### 公考视图

| 视图名 | 设置 |
| --- | --- |
| 全部 | 无筛选 |
| 报名中 | `报名状态 = 报名中` |
| 本周截止 | `距截止天数 >= 0` 且 `<= 7`，并且不为空 |
| 选调生 | `招录类型 = 选调生` |
| 我的地区 | `地区` 包含“山东 / 河北 / 辽宁 / 黑龙江 / 天津”中的任意一个 |
| 应届可报 | `应届可报` 已勾选 |
| 按招录类型分组 | 按 `招录类型` 分组 |

### 秋招视图

| 视图名 | 设置 |
| --- | --- |
| 全部 | 无筛选 |
| 本周截止 | `距截止天数 >= 0` 且 `<= 7`，并且不为空 |
| 国企央企 | `企业性质 = 国企` 或 `央企` |
| 生物医药·AI | `行业` 包含“生物医药”或“AI” |
| 按企业性质分组 | 按 `企业性质` 分组 |

## 6. 在 S1 配置密钥并试运行

进入 S1 上的项目目录，确认两份源文件存在：

```bash
ls -l /var/www/hot-gap/data/gongkao.json /var/www/hot-gap/data/qiuzhao.json
```

在项目根目录未跟踪的 `.env` 中加入以下变量。不要给 App Secret 加多余空格；真实值不要
写进 `.env.example`、测试 fixture 或部署文档。

```dotenv
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=在开放平台复制的真实值
SITE_DATA_DIR=/var/www/hot-gap/data
FEISHU_SYNC_CONFIG=config/feishu_sync.yaml
```

可选告警变量沿用现有配置：

```dotenv
FEISHU_WEBHOOK=
BARK_URL=
```

某张表连续两次同步失败后，若至少配置了一个告警地址，脚本会发出告警；计数保存在
`SITE_DATA_DIR/.feishu_sync_failures.json`。任意一张表失败不会阻止另一张继续同步，最终进程会
返回非零退出码，方便 cron 记录失败。

先手动运行一次：

```bash
cd /path/to/hot-gap-aggregator
.venv/bin/python -m app.export_qiuzhao
.venv/bin/python -m app.sync_feishu
```

如需使用人工抓取的秋招快照，把标准化 JSON 上传为：

```text
/var/www/hot-gap/data/qiuzhao_wanqing.json
```

随后运行 `app.export_qiuzhao`。只要该文件存在，定时任务就会继续用它生成
`qiuzhao.json`；删除或移走该文件后，导出器才会恢复使用 `jobs.json`。快照必须包含
`items` 数组，每条至少有非空的 `company_name` 和 `position`。

正常日志会分别显示 `gongkao sync complete`、`qiuzhao sync complete` 以及新增、更新、删除、
跳过数量。然后回飞书抽查日期、超链接、单选和复选框字段。

常见错误：

- `FieldNameNotFound`：字段名称不一致，按本文清单和 YAML 右侧逐字检查。
- `Forbidden`：没有开 `bitable:app`、应用权限尚未审批，或没有把文档应用设成可编辑协作者。
- `app_token/table_id` 相关错误：把 `view` ID 当成了 table ID，或复制了另一份多维表格的 token。
- 某条源数据被跳过：公考缺 `extra.id`，或秋招缺公司名称/岗位，日志会写明源数据序号。

## 7. 配置 crontab

执行 `crontab -e`，加入：

```cron
0 7,13,19 * * * /usr/local/sbin/hot-gap-feishu-refresh >> /var/log/hot-gap-feishu-sync.log 2>&1
```

确保 cron 用户能读取项目 `.env`、`config/feishu_sync.yaml` 和两份 JSON，并能写入
`/var/www/hot-gap/data/` 中的失败计数文件。服务器时区应设为 `Asia/Shanghai`；脚本本身也固定按
北京时间计算“今天”和“距截止天数”。

`hot-gap-feishu-refresh` 使用文件锁避免 7:00 的本机抓取回调与 S1 cron 同时写飞书，并在每次
同步前从 `/var/lib/hot-gap/qiuzhao_wanqing.json` 恢复秋招工作副本。不要再保留直接调用
`app.export_qiuzhao && app.sync_feishu` 的旧 cron 行。

## 手动添加数据：来源必须选“手动”

直接在飞书中新增行，填写业务字段，并把 `来源` 选为 `手动`。`同步ID` 可以留空。
同步脚本只索引、更新和删除 `来源 = 自动` 的行，因此手动行即使与自动行的同步 ID 相同，也会
完全绕开。不要把希望长期保留的手动行误选成“自动”。

自动行由源 JSON 决定：源中新增会创建，内容变化才更新，源中消失会删除。仅“更新时间”变化
不会触发无意义更新；创建或业务字段发生变化时，更新时间会写成本次同步时刻。

## 分享权限在哪里设置

打开多维表格后，点右上角“分享”：

- 在“添加协作者”中控制具体成员或群组的查看/编辑权限。
- 在“链接分享”中控制组织内或互联网上通过链接访问的范围。
- 自建应用的写权限不在这里设置，仍需通过“⋯ 更多 → 添加文档应用”授予可编辑权限。

建议默认关闭互联网匿名编辑，只给确实需要维护手动行的成员编辑权限。

## 每天 7 点自动刷新婉清秋招快照

来源表只允许网页查看，不能通过你的自建应用 OpenAPI 导出。因此抓取任务运行在 Windows
电脑上，使用独立的 Playwright 浏览器配置保存登录会话；仓库、`.env` 和服务器都不保存来源表
Cookie。脚本会同时留下当天页面截图用于排错，但结构化数据直接读取页面背后的表格接口，避免
截图 OCR 截断岗位和链接。

首次安装依赖并登录：

```powershell
cd "C:\Users\Administrator\Desktop\简历\hot-gap-aggregator"
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m app.capture_wanqing --login `
  --output "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing\qiuzhao_wanqing.json" `
  --profile-dir "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing\browser-profile" `
  --state-dir "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing"
```

弹出的窗口中只需登录一次，并确认能看到 `27届秋招🍁`，再回终端按 Enter。Codex 应用内的
“婉清秋招每日同步”自动任务会在每天北京时间 7:00 调用 `scripts/run_wanqing_sync.ps1`：
读取并去重最新 500 条、保存截图、上传到 S1 的待处理文件，然后调用受限命令把快照原子保存到
`/var/lib/hot-gap/qiuzhao_wanqing.json`，复制为
`/var/www/hot-gap/data/qiuzhao_wanqing.json`，最后同步到自己的飞书表。
电脑在 7:00 必须开机且 Codex、Chrome 可正常运行；未运行时自动任务会在恢复后按应用机制
处理。也可以随时手动执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_wanqing_sync.ps1
```

`/var/lib/hot-gap` 是持久真源，不受站点数据刷新影响；服务器每次 7/13/19 同步前都会从这里
恢复工作副本。安全保护：抓取失败、登录过期、字段变化或有效记录少于 450 条时，脚本返回失败并保留上一次
快照，不上传空文件，也不会导致飞书误删。连续失败且配置 `FEISHU_WEBHOOK` 或 `BARK_URL`
时会告警。日志和截图位于 `%LOCALAPPDATA%\hot-gap-aggregator\wanqing`。

S1 需由 root 一次性安装受限刷新命令（只允许执行这一条固定命令）：

```bash
install -o root -g root -m 755 /opt/hot-gap-aggregator/deploy/server/hot-gap-feishu-refresh /usr/local/sbin/hot-gap-feishu-refresh
install -o root -g root -m 440 /opt/hot-gap-aggregator/deploy/server/hot-gap-feishu-refresh.sudoers /etc/sudoers.d/hot-gap-feishu-refresh
visudo -cf /etc/sudoers.d/hot-gap-feishu-refresh
```

该辅助命令只进入 `/opt/hot-gap-aggregator` 执行秋招导出与飞书同步，不访问
`/opt/cuotiben`。服务器原有 `7,13,19` 同步任务可保留；文件锁会避免两个同步进程同时写表。

## 更换 App Secret

1. 在飞书开放平台打开该自建应用的“凭证与基础信息”，重置或重新生成 App Secret。
2. 登录 S1，仅替换项目 `.env` 中 `FEISHU_APP_SECRET` 的值；不要改 YAML，也不要提交 `.env`。
3. 手动运行 `.venv/bin/python -m app.sync_feishu` 验证两张表均成功。
4. Secret 更新后下次 cron 会自动使用新值；这是一次性进程，无需重启常驻服务。

如果 Secret 曾进入 Git、日志、截图或聊天，应立即重置，而不是只删除泄露位置。
