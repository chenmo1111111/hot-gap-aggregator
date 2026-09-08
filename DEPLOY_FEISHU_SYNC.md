# S1：公考 + 秋招同步到飞书多维表格

飞书写入任务只部署在 S1，最终读取 `/var/www/hot-gap/data/gongkao_feishu.json` 和
`/var/www/hot-gap/data/qiuzhao.json`。`gongkao_feishu.json` 由原有 `gongkao.json` 与本地浏览器
抓取的 `gongkao_sheet.json` 去重合并生成，原有公考源优先且不会被改写。默认情况下，秋招文件由 S1 上现有的 `jobs.json`
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
.venv/bin/python -m app.export_gongkao
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
同步前从 `/var/lib/hot-gap/qiuzhao_wanqing.json` 和 `/var/lib/hot-gap/gongkao_sheet.json`
恢复两个网页源的工作副本。不要再保留直接调用
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

## 对外分享的精简表

内部表保留同步 ID、来源和状态等维护字段；另建一份只读分享的多维表格作为公开展示层。
公开表不保存同步 ID：公考按公告链接识别记录，秋招按“公司名称＋招聘岗位”识别记录。
它直接读取同一批源数据，不从内部表二次抓取。公考使用要点增强后的
`gongkao_enriched.json`，秋招继续使用 `qiuzhao.json`。

公开公考表字段为：`公告标题`（主字段）、`日期`、`首次收录`、`类别`、`招聘人数`、`截止日期`、
`省份`、`链接`、`报名状态`、`距截止天数`、`细分类别`、`限户籍`、`限专业`、`学历要求`、
`限应届`、`服务期`、`招录院校范围`、`备注`。公开秋招表字段为：`公司名称`（主字段）、`日期`、`企业性质`、
`行业`、`招聘岗位`、`工作地点`、`学历要求`、`届次`、`是否笔试`、`投递链接`、
`公告链接`、`备注`。

新建空白公开表并把自建应用添加为可编辑文档应用后，在
`config/feishu_public_sync.yaml` 填写它的 app token 和两个 table ID。第一次执行时，脚本在空表
中初始化上述精简结构。以后增加字段时只会追加缺失列，不删除旧列、不重建表，也不会清空已有
记录；同名字段类型不正确时会停止并报错，避免误改。公开表是展示副本，不要直接维护手动行；
对外分享权限应设为“可阅读”。

```bash
.venv/bin/python -m app.sync_feishu_public
```

服务器辅助命令会在内部表同步成功后继续同步公开表，因此 Windows 每天 05:00 抓取完成后会
立即更新两套表，S1 的 07:00、13:00、19:00 任务也会刷新公开表。

### 公开公考表的 9 个业务视图

飞书视图由用户手工建立，脚本只维护字段和数据，不创建或覆盖视图。“全部”是默认视图，另外
建立以下 9 个业务视图。`报名状态` 的“剩1天”到“剩5天”仍属于进行中，只是单独突出紧急度。

| 视图名 | 筛选与排序条件 |
| --- | --- |
| 今日必做 | `报名状态` 不等于 `未开始` 且不等于 `已截止`；`距截止天数` 不为空、`>= 0`、`<= 5`；按 `距截止天数` 升序 |
| 进行中 | `报名状态` 是 `报名中 / 剩1天 / 剩2天 / 剩3天 / 剩4天 / 剩5天` 中任一个 |
| 本周截止 | `报名状态` 不等于 `已截止`；`距截止天数` 不为空、`>= 0`、`<= 7`；按天数升序 |
| 选调生 | `细分类别 = 选调生`；需要时再按 `招录院校范围` 搜索学校层次关键词 |
| 国企央企 | `细分类别 = 国企` 或 `央企` |
| 事业单位 | `细分类别 = 事业单位` |
| 银行 | `细分类别 = 银行` |
| 我的地区 | `省份` 包含用户关心的省份；多个省份使用“任一条件满足” |
| 已结束 | `报名状态 = 已截止` |

`招录院校范围` 是面向所有买家的公告级概括，不替代院校、专业、学历、政治面貌等资格复核。
以后可在同一个 Base 新建一张「选调院校名单」参考表，字段为 `省份 | 高校清单`，买家使用
Ctrl-F 搜索自己的学校。`config/xuandiao_schools.yaml` 中的 `my_school` 仍为网站个人版保留，
不再同步到公开飞书表。
已截止记录不会因为过期而删除；“进行中”视图负责把它们隐藏，“已结束”视图负责归档查看。

### 公考公开表升级后的一次性手动调整

首次运行新版同步后，脚本会自动新增 `首次收录`、`招录院校范围`，并删除旧的 `笔试科目`、
`本校可报`。飞书开放 API 不能可靠设置已有列的可视顺序及视图排序，请在飞书网页手动完成：

1. 在「全部」视图把 `备注` 列拖到最右侧。
2. 在「全部」视图点击「排序」，添加 `首次收录`，选择降序（最新日期在上）。
3. 「今日必做」仍按 `距截止天数` 升序；其它业务视图保留各自原排序，不要套用“全部”的排序。
4. 若升级前同步异常导致旧列仍存在，确认新列已有数据后，手动删除 `笔试科目`、`本校可报`。

首次启用时，历史存量会统一记为启用当天；从下一批新公告开始即可看到新数据稳定排在顶部。
日期保存在 `/var/lib/hot-gap/gongkao-enrichment.db` 的 `gongkao_first_seen` 表，后续同步不会改写。

### 公告要点增量提取与选调院校配置

服务器先生成 `gongkao_feishu.json`，再运行：

```bash
.venv/bin/python -m app.pipeline.gongkao_enrich
```

结果写入 `gongkao_enriched.json`，公开表从该文件同步。粉笔热门公告列表按
`offset=0/50/100/150&num=50` 拉取四页并按公告 ID 去重，再通过详情接口
`deviceType=3&app=web&av=100&hav=100&kav=100&client_context_id=` 获取 UTF-8 HTML，提取
`#content` 正文。`export_gongkao` 同时合并服务器的 `server-gongkao.json`；其中选调公告、国家
公务员局/税务海关公告和高校就业网选调公告，会直接访问其公开 HTTP(S) 公告 URL，自动识别
UTF-8/GB18030 编码并从常见正文容器中取正文。URL 来源使用规范化 URL 的 SHA-256 作为缓存键，
不向目标站发送飞书 Cookie；内网地址及 PDF/Word 等非 HTML 页面会拒绝抓取并标为“未提取”。
DeepSeek 结果缓存在
`/var/lib/hot-gap/gongkao-enrichment.db` 的 `gongkao_enrichment` 表，同一条、同一源内容不会重复
计费；提取结构版本或内容指纹变化才重新提取。选调生公告额外提取 `招录院校范围`；普通公告
不生成该字段。没有 `DEEPSEEK_API_KEY` 或单条请求失败时标为“未提取”并继续
后续飞书同步。确需重试失败项时运行：

```bash
.venv/bin/python -m app.pipeline.gongkao_enrich --retry-failed
```

选调学校名单在 `config/xuandiao_schools.yaml`。只有官方公告或附件明确列出的院校才进入名单；
没有可可靠提取的名单时保持空数组，再根据公告中的 `双一流 / 985 / 211` 文字做兜底。配置中的
公告源可单独重跑提取（PDF 源要求系统已安装 `pdftotext`）：

```bash
.venv/bin/python -m app.update_xuandiao_schools --dry-run
.venv/bin/python -m app.update_xuandiao_schools --province 辽宁
```

### 每日 Top10 飞书群卡片

在 S1 未跟踪的 `.env` 中配置买家群自定义机器人地址：

```dotenv
FEISHU_DIGEST_WEBHOOK=机器人真实 webhook
# 机器人若启用“签名校验”再填写；不要提交仓库
FEISHU_DIGEST_SIGN_SECRET=
```

`app.notify_gongkao_digest` 在公开表同步成功后运行。每天最多成功发送一条；普通公告推送一次，
进入剩余 3 天以内后允许再提醒一次。去重记录保存在
`/var/lib/hot-gap/gongkao-digest.db`，Webhook 未配置时仅记日志并正常退出。手动预览/补发可用
`--force`，但这会真实向群发送消息：

```bash
.venv/bin/python -m app.notify_gongkao_digest --force
```

服务器刷新顺序应为：`export_qiuzhao` → `export_gongkao` → `gongkao_enrich` → 内部表同步 →
公开表同步 → `notify_gongkao_digest`。本次新增步骤只增强公考公开表，秋招公开表字段和映射不变。

## 每天 5 点自动刷新秋招和公考网页源

两个来源表只允许网页查看，不能通过你的自建应用 OpenAPI 导出。因此抓取任务运行在 Windows
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

弹出的窗口中只需登录一次，并确认能看到 `27届秋招🍁`，再回终端按 Enter。同一个专用浏览器
配置也用于公考普通表格。首次确认公考表账号可访问时运行：

```powershell
.venv\Scripts\python.exe -m app.capture_gongkao_sheet --login `
  --output "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing\gongkao_sheet.json" `
  --profile-dir "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing\browser-profile" `
  --state-dir "$env:LOCALAPPDATA\hot-gap-aggregator\wanqing"
```

脚本识别到可访问账号后会读取整张表；公告链接列即使在屏幕右侧不可见也能完整取得。然后使用
“以管理员身份运行”的 PowerShell 注册每天 5:00 自动唤醒任务：

```powershell
cd "C:\Users\Administrator\Desktop\简历\hot-gap-aggregator"
powershell -ExecutionPolicy Bypass -File .\scripts\install_wanqing_task.ps1
```

计划任务 `HotGap-Wanqing-Feishu-0700`（名称为兼容旧安装而保留）会在每天北京时间 5:00
唤醒电脑并调用
`scripts/run_wanqing_sync.ps1`：
先刷新秋招并读取最新 500 条，再刷新公考表并读取全部有效公告，分别保存诊断截图、上传到 S1
待处理文件，然后调用受限命令把快照原子保存到 `/var/lib/hot-gap/`。服务器生成秋招文件及
`gongkao_feishu.json` 后，再同步到自己的飞书表。一张来源抓取失败时，另一张仍可更新，失败来源
继续使用服务器上的上一次有效快照。
电脑在 7:00 必须开机且用户仍保持登录；允许唤醒定时器在交流供电下应为启用状态。电脑可以
睡眠，任务会将其唤醒；电脑已关机或用户已注销时不能运行。错过执行时间后，任务会在系统恢复
可用时尽快补跑。无需预先打开或手动刷新飞书页面：每次运行都会重新访问页面、获取最新表版本
并请求最新记录。也可以随时手动执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_wanqing_sync.ps1
```

`/var/lib/hot-gap` 是持久真源，不受站点数据刷新影响；服务器每次 7/13/19 同步前都会从这里
恢复工作副本。安全保护：抓取失败、登录过期、字段变化、秋招有效记录少于 450 条或公考有效公告
少于 500 条时，脚本返回失败并保留上一次
快照，不上传空文件，也不会导致飞书误删。连续失败且配置 `FEISHU_WEBHOOK` 或 `BARK_URL`
时会告警。日志和截图位于 `%LOCALAPPDATA%\hot-gap-aggregator\wanqing`。

S1 需由 root 一次性安装受限刷新命令（只允许执行这一条固定命令）：

```bash
install -o root -g root -m 755 /opt/hot-gap-aggregator/deploy/server/hot-gap-feishu-refresh /usr/local/sbin/hot-gap-feishu-refresh
install -o root -g root -m 440 /opt/hot-gap-aggregator/deploy/server/hot-gap-feishu-refresh.sudoers /etc/sudoers.d/hot-gap-feishu-refresh
visudo -cf /etc/sudoers.d/hot-gap-feishu-refresh
```

该辅助命令只进入 `/opt/hot-gap-aggregator` 执行两类导出与飞书同步，不访问
`/opt/cuotiben`。服务器原有 `7,13,19` 同步任务可保留；文件锁会避免两个同步进程同时写表。

## 更换 App Secret

1. 在飞书开放平台打开该自建应用的“凭证与基础信息”，重置或重新生成 App Secret。
2. 登录 S1，仅替换项目 `.env` 中 `FEISHU_APP_SECRET` 的值；不要改 YAML，也不要提交 `.env`。
3. 手动运行 `.venv/bin/python -m app.sync_feishu` 验证两张表均成功。
4. Secret 更新后下次 cron 会自动使用新值；这是一次性进程，无需重启常驻服务。

如果 Secret 曾进入 Git、日志、截图或聊天，应立即重置，而不是只删除泄露位置。
