<div align="center">

# 推特转发 · Twitter Forwarder

**MaiBot 的 X（推特）自动转发插件** —— 按设定间隔轮询订阅的推主，有新推就自动搬进群：
正文、配图、**视频本体**、自动翻译、链接正文一条龙；所有参数都能在聊天里用斜杠命令直接改。

[![MaiBot](https://img.shields.io/badge/MaiBot-1.2.x-4f7cff)](https://github.com/MaiM-with-u/MaiBot)
[![Plugin SDK](https://img.shields.io/badge/Plugin%20SDK-2.8%2B-2ea043)](https://docs.mai-mai.org/plugin/)
[![version](https://img.shields.io/badge/version-1.6.2-blue)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-GPL--3.0--or--later-orange)](LICENSE)

[功能特性](#-功能特性) · [安装](#-安装) · [命令](#-斜杠命令) · [配置](#%EF%B8%8F-配置说明) · [安全](#-安全说明) · [常见问题](#-常见问题) · [更新日志](CHANGELOG.md)

</div>

---

## 📖 简介

插件通过 [FxTwitter / FxEmbed](https://github.com/FxEmbed/FxEmbed) 的公开接口轮询推主时间线，
按推文 ID 去重，把新推文打包成一条**合并转发（聊天记录）**发到订阅它的聊天流。

| 用途 | 接口 |
| --- | --- |
| 时间线（主） | `https://api.fxtwitter.com/2/profile/<handle>/statuses` |
| 时间线（兜底） | `https://fxtwitter.com/<handle>/feed.atom.xml` |
| 推主资料 | `https://api.fxtwitter.com/<handle>` |
| 图片 / 视频下载 | `pbs.twimg.com` / `video.twimg.com`（**国内必须走代理**） |

状态保存在 `data/plugins/polarbear.twitter-forwarder/state.json`（订阅关系、去重书签、运行时覆盖）。

## ✨ 功能特性

- **定时轮询**：默认每 10 分钟检查一次所有订阅推主，间隔可用 `/tw_interval` 临时改。
- **不补推历史**：新订阅时把当前时间线记为基线，只推之后出现的新推文，不会刷屏。
- **一条聊天记录装多条推文**：一轮的多条新推按时间线打包，**每条推文一个节点、最新的排在最上面**。
- **视频本体**：原推带视频就尽量把 mp4 搬过来（内联直发 / 借道 docker 容器发本地路径），搬不动才退回封面 + 链接。
- **自动翻译**：正文与引用自动翻成中文，**译文直接替换原文**；翻译失败自动保留原文，绝不影响推送。
- **链接正文**：推文里的链接会把页面正文抓出来贴在下面并一起翻译（Steam 公告走官方 RSS 精确匹配）。
- **全命令管理**：订阅、退订、暂停、间隔、测试推送、重建基线都能在群里用斜杠命令完成。
- **LLM 工具**：注册了只读工具 `twitter_latest(handle, count)`，AI 可以直接查"某某最近发了什么"。
- **分层回退**：视频失败 → 封面 + 链接；合并转发失败 → 逐条图文；翻译失败 → 保留原文。
  **绝不因为格式或网络问题丢推文。**
- **默认收紧的安全边界**：跨聊天命令默认仅管理员、链接抓取默认拦截内网地址（防 SSRF）、
  默认跳过被标记为敏感的推文。详见[安全说明](#-安全说明)。

## 📦 安装

### 前置要求

| 依赖 | 要求 |
| --- | --- |
| MaiBot（MaiCore） | `1.2.0` ~ `1.2.x` |
| 插件 SDK | `maibot_sdk >= 2.5.0`（随 MaiBot 提供） |
| Python 依赖 | 仅 `aiohttp`（MaiBot 自带），**无第三方包依赖** |
| 可选增强 | `trafilatura` / `beautifulsoup4` / `readability-lxml`（装了链接正文提取更干净，不装也能跑） |
| HTTP 代理 | 下载推文图片/视频用，例如 `http://127.0.0.1:7890`（Clash / mihomo） |

### 步骤

1. 把本仓库放到 MaiBot 的插件目录下：

   ```bash
   cd /path/to/MaiBot/plugins
   git clone https://github.com/niconieryr/maibot-twitter-forwarder.git
   ```

   > 目录名不重要，插件身份由 `_manifest.json` 里的 `id` 决定。
   >
   > **仓库里没有 `config.toml`**：插件首次加载时，Runner 会根据插件内置的
   > `config_model` 自动生成这个文件（带中文注释和默认值），所以不要把实例配置提交回仓库，
   > 免得以后 `git pull` 撞冲突。

2. 确认生成出来的 `config.toml` 里 `[plugin] enabled = true`，并检查 `[twitter] proxy`
   指向本机可用的代理——**接口本身可以直连，但图片和视频在 `pbs.twimg.com` / `video.twimg.com`，
   不走代理就只能发文字**。

3. 重启 MaiBot（或在 WebUI 热重载插件），日志里应出现：

   ```text
   推特转发插件已启动：订阅 0 个推主，轮询间隔 10 分钟，代理=http://127.0.0.1:7890，视频模式=auto
   链接正文提取器：trafilatura/bs4/readability
   ```

   > 如果第二行显示 `链接正文提取器：无`，说明可选增强没装，插件仍能正常工作，
   > 只是正文提取会退化成正则清洗 + `og:description`。想装：
   > `uv pip install trafilatura beautifulsoup4 readability-lxml`（注意别装进系统 Python）。

4. 在群里订阅一个推主：

   ```text
   /tw_sub elonmusk
   ```

   订阅成功后会把该推主最新一条推文推过来（可用 `push.push_latest_on_subscribe` 关掉）。

5. **（重要）配置管理员**：跨聊天命令（`/tw_all`、`/tw_del`、`/tw_reset`、`/tw_check`、
   `/tw_interval`）默认**只有管理员和本地操作员**能用，见[安全说明](#-安全说明)。
   想让自己的 QQ 能用，把它们填进 `command.admins`：

   ```toml
   [command]
   cross_chat_admin_only = true
   admins = ["123456789"]
   ```

6. **（可选，想收大视频必做）** 把 QQ 适配器的动作超时调大，见
   [视频怎么发 · 关键：把适配器的 action 超时调大](#关键把适配器的-action-超时调大大视频必做)。

## 💬 斜杠命令

带 🔒 的命令会查看或修改**全局订阅 / 轮询状态**，默认只有管理员与本地操作员能用
（`command.cross_chat_admin_only`）；其余命令只影响当前聊天。

| 命令 | 范围 | 说明 |
| --- | --- | --- |
| `/tw_sub <用户名…>` | 当前聊天 | 订阅推主到当前聊天，支持 `@名字`、`https://x.com/名字`、推文链接 |
| `/tw_unsub <用户名…>` | 当前聊天 | 取消当前聊天对该推主的订阅 |
| `/tw_list` | 当前聊天 | 查看当前聊天的订阅 |
| `/tw_on` / `/tw_off` | 当前聊天 | 恢复 / 暂停当前聊天的推送（订阅关系保留） |
| `/tw_test <用户名> [条数]` | 当前聊天 | 把最新推文立刻推过来预览，不影响订阅状态 |
| `/tw_status` | 只读 | 查看运行状态（订阅数、上次轮询、数据源、代理） |
| `/tw_help` / `/tw` | 只读 | 显示帮助 |
| 🔒 `/tw_all` | 全局 | 查看**所有聊天**的订阅、推送目标与最近错误 |
| 🔒 `/tw_check [用户名]` | 全局 | 立刻检查一次，不带参数表示检查全部订阅 |
| 🔒 `/tw_interval [分钟\|reset]` | 全局 | 查看 / 设置轮询间隔，`reset` 恢复配置文件里的值 |
| 🔒 `/tw_reset <用户名>` | 全局 | 重建基线，下次轮询不补推历史（影响所有订阅它的聊天） |
| 🔒 `/tw_del <用户名>` | 全局 | 彻底删除该推主在**所有聊天**的订阅 |

半角 `/` 和全角 `／` 都可以，命令前带不带 `@机器人` 都能识别。
用户名打错时会提示最接近的真实账号（例如 `limbus_company_b` 超长 → 提示 `LimbusCompany_B`）。
非管理员调用 🔒 命令时会得到明确提示：`这条命令会影响所有聊天的订阅或全局轮询状态…`。

## ⚙️ 配置说明

`config.toml` 由 Runner 在首次加载时**根据插件内置的 config_model 自动生成**（带中文注释），
也可以在 WebUI 的插件配置页里改；**这个文件属于实例配置，不要提交回仓库**（本仓库里没有它）。
聊天命令产生的改动保存在 `state.json`，**优先级高于 `config.toml`**，
用 `/tw_interval reset` 之类可以回到配置文件的值。

<details>
<summary><b>常用配置速查</b></summary>

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poll.interval_minutes` | `10` | 轮询间隔（分钟） |
| `poll.max_tweets_per_poll` | `3` | 每个推主每轮最多推几条，剩下的下一轮继续 |
| `twitter.proxy` | `http://127.0.0.1:7890` | 下载图片/视频用的代理 |
| `push.extra_streams` | `[]` | 额外固定推送目标（聊天流 session_id） |
| `push.include_reposts` | `true` | 是否推送转推 |
| `push.include_replies` | `false` | 是否推送回复 |
| `push.skip_sensitive` | **`true`** | 是否跳过被接口标记为「可能敏感」的推文（默认跳过） |
| `media.video_mode` | `auto` | `auto` / `thumbnail` / `link` |
| `media.inline_video_mb` | `10.0` | 内联 base64 直发的视频上限（别超过 11） |
| `media.max_video_mb` | `300.0` | 视频下载上限，超过就回退封面 + 链接 |
| `media.big_video_docker_route` | `true` | 大视频是否借道 docker 容器 |
| `media.docker_container` | `snowluma` | 大视频路由目标容器名 |
| `media.video_keep_seconds` | `180` | 发完后容器内视频保留多少秒才删（0=立刻删） |
| `display.use_forward` | `true` | 是否打包成合并转发 |
| `display.batch_forward` | `true` | 多条推文是否合并成一条聊天记录 |
| `display.video_in_forward` | `true` | 视频是否放进聊天记录（否则单独成条） |
| `display.max_text_chars` | `600` | 正文截断长度，0 = 不截断 |
| `translation.enabled` | `true` | 是否自动翻译 |
| `translation.model_task` | `replyer` | 用哪个模型任务翻译（`replyer` / `utils` / …） |
| `translation.target_lang` | `简体中文` | 翻译目标语言 |
| `link.enabled` | `true` | 是否抓取链接正文 |
| `link.max_chars` | `3000` | 链接正文长度上限（0 = 不限） |
| `link.allow_private_hosts` | **`false`** | 是否允许抓取指向本机/内网的链接（SSRF 防护开关） |
| `command.cross_chat_admin_only` | **`true`** | 跨聊天命令是否仅管理员可用 |
| `command.admin_only` | `false` | 是否所有命令都仅管理员可用 |
| `command.admins` | `[]` | 管理员 QQ 号列表 |
| `command.admin_only` | `false` | 是否限制命令使用者 |
| `command.admins` | `[]` | 管理员 QQ 号 |

</details>

<details>
<summary><b>完整配置段一览</b></summary>

| 配置段 | 作用 |
| --- | --- |
| `[plugin]` | `enabled`、`config_version` |
| `[poll]` | 轮询间隔、首次延迟、超时、重试、拉取条数、每轮推送上限、并发 |
| `[twitter]` | API/feed 地址、代理、是否给 API 也走代理、User-Agent |
| `[push]` | 额外推送目标、转推/回复开关、订阅时是否推最新一条、敏感内容过滤 |
| `[media]` | 图片开关/张数/尺寸/大小/超时，视频模式/内联上限/下载上限/超时/docker 借道/保留时长 |
| `[display]` | 合并转发、批量打包、聊天记录体积上限、视频进节点、昵称、作者行、分隔线、链接与统计、正文截断 |
| `[translation]` | 翻译开关、模型任务、目标语言、引用翻译、跳过条件、长度上限、并发、超时、提示词 |
| `[link]` | 链接正文开关、展开条数、长度上限、分段翻译字数、超时、页面大小、代理、是否翻译、Steam 语言、**SSRF 私网开关** |
| `[command]` | 是否所有命令仅管理员、**跨聊天命令是否仅管理员**、管理员列表 |

每个字段在生成的 `config.toml` 里都有中文注释，直接看文件即可。

</details>

## 🔐 安全说明

### 1. 命令权限：跨聊天操作默认收紧

`/tw_all`、`/tw_del`、`/tw_reset`、`/tw_check`、`/tw_interval` 会**看到或改到别的聊天**，
默认由 `command.cross_chat_admin_only = true` 限制为**仅管理员与本地操作员**：

```toml
[command]
cross_chat_admin_only = true
admins = ["你的QQ号"]
```

- **本地操作员**指 MaiBot 控制台 / WebUI 发来的指令（平台为 `bot_console`），始终放行。
- 只影响当前聊天的命令（`/tw_sub`、`/tw_unsub`、`/tw_list`、`/tw_on|off`、`/tw_test`）
  不受此限制。
- 想恢复"谁都能用"的老行为，把 `cross_chat_admin_only` 设成 `false`，
  或用更粗的 `command.admin_only = true` 把**所有**命令都锁给管理员。

### 2. 链接预览的 SSRF 防护

推文正文是**别人写的内容**，里面的链接可能指向 `http://127.0.0.1:xxxx`、
`http://192.168.x.x`、`http://169.254.169.254`（云 metadata）、`http://100.64.x.x`（CGNAT / Tailscale）
这类地址；插件会把抓到的正文发进群，等于给了外部一个读内网的通道。因此：

- 抓取前先做校验：协议必须是 `http(s)`；主机名不能是 `localhost` / `*.local` / `*.internal`
  这类名字；域名会**解析成 IP 逐个检查**，只要有一个不是公网地址（`not is_global`）就整条丢弃。
- **每次跳转都重新校验**：手动跟随 3xx（最多 5 跳），避免"公网域名 302 到内网"绕过。
- 被拦下的链接只写一条日志（`链接内容跳过（SSRF 防护）: …`），推文照常推送，只是不带链接内容块。
- 需要抓内网链接（例如自建 Wiki）时显式打开 `link.allow_private_hosts = true`。

> 已知边界：校验在 DNS 解析后进行，理论上存在 DNS rebinding（解析时公网、连接时内网）的窗口；
> 对"把内容转发进聊天"这种低带宽场景，收益远小于风险，所以按主流做法先做解析校验。
> 图片 / 视频的下载地址由 FxTwitter 接口给出（`pbs.twimg.com` / `video.twimg.com`），不受推文内容控制。

### 3. 敏感内容默认不转发

`push.skip_sensitive = true`（默认）会跳过被接口标记为 `possibly_sensitive` 的推文，
避免群里突然出现不宜内容。确实想全都要，再改成 `false`。

### 4. 其他

- 抓取链接正文时的页面下载上限 `link.max_page_mb`（默认 2MB）、超时 `link.timeout_seconds`（默认 15s）。
- 翻译 / 链接抓取失败都只降级、不中断投递，不会因为外部内容把推文卡住。

## 🧭 行为说明

- **去重**：按推文 ID 记录已推过的内容，重启不重复推；投递失败的推文不记为已见，下一轮自动补推。
- **一条聊天记录装多条推文**：一轮轮询到的多条新推文打包成**一条合并转发**，
  **每条推文占其中一个节点**，**最新的推文排在最上面**；单条推文同样是「一条聊天记录 + 一个节点」。
- **视频推文占两个相邻节点**：QQ 协议规定「video 必须是消息里唯一的元素」
  （原文 `message element "video" must be the only segment in a message`），
  所以视频推文先是正文节点、紧跟一个只有视频的节点，整体仍在同一条聊天记录里。
  > 更深一层的「节点里再套聊天记录」不被适配器支持（会打
  > `SnowLuma 跳过无法转换的出站消息段: type=forward`），整条消息会发送失败。
- **自动拆分**：图片总量超过 `display.batch_max_mb` 或节点数超限时，自动拆成多条聊天记录。
- **同一轮上限**：`poll.max_tweets_per_poll` 之外的推文不会被标记为已见，下一轮继续推，不会丢。
- **转推 / 回复**：默认推转推、不推回复，可用 `push.include_reposts`、`push.include_replies` 调整。

## 🎨 消息排版

```text
🐦 Limbus Company (@LimbusCompany_B) · 2026-09-11 15:07     ← 作者头
────────────────                                            ← 分隔线（可关）
[关于主线故事第10章抢先游玩的临时追加公告]                    ← 正文（已翻译）
#LCB #림버스 #LimbusCompany #リンバス

📄 链接内容 · Notice: Elaboration Regarding Canto 10 ...     ← 链接内容（前面空一行）
你好，我是 Project Moon 的总监 Kim Jihoon。
...

🔗 https://x.com/LimbusCompany_B/status/2098428281007829309  ← 尾部（前面空一行）
❤️ 5382 · 🔁 972 · 💬 61 · 👁 46.6万
```

| 段 | 内容 | 相关配置 |
| --- | --- | --- |
| 作者头 | 转推来源、`🐦 名字 (@handle) · 时间`、分隔线 | `display.show_author`、`display.header_divider` |
| 正文 | 推文正文（已翻译）、`┌ 引用 …`、`🖼 配图 N 张` | `display.max_text_chars` |
| 链接内容 | `📄 链接内容 · 标题` + 正文 | `[link]` |
| 尾部 | `🔗 原推链接`、`❤️ 点赞 · 🔁 转发 · 💬 回复 · 👁 浏览` | `display.show_link`、`display.show_stats` |

已经抓出正文的链接会从正文里去掉，避免和下面的链接内容重复（`display.hide_expanded_url`）；
正文里的多余空行会被收敛。一条聊天记录里每条推文一个节点，**最新的排在最上面**。

## 🎬 视频怎么发

推文带视频时，插件会解析 fxtwitter 给出的全部 mp4 变体（360p / 480p / 720p / 1080p / 4K），
**逐个 HEAD 问真实大小**，挑上限内码率最高的那个，再按大小选路线：

| 视频大小 | 走法 | 说明 |
| --- | --- | --- |
| ≤ `media.inline_video_mb`（默认 10MB） | 内联 base64 直发 | 最快最稳。受插件 IPC 单帧 16MB 限制，代码里硬夹到 11MB |
| ≤ `media.max_video_mb`（默认 300MB） | 下载 → 拷进 SnowLuma 容器 → 用容器内路径发送 | 需要 docker 权限；发完延迟删除容器内文件和本地临时文件 |
| 超过 `media.max_video_mb`，或下载/发送失败 | 回退「封面 + 原推链接」 | **不会因为视频失败而整条推文丢失** |

> 容器借道依赖「运行 MaiBot 的用户能执行 `docker` 命令」，容器名由 `media.docker_container`
> 指定（默认 `snowluma`）。换了容器名或没有 docker 权限，把它设成 `false`，超限视频就走封面回退。

### 关键：把适配器的 action 超时调大（大视频必做）

`snowluma-adapter` 的默认动作超时是 **10 秒**（`plugins/maibot-team_snowluma-adapter/config.toml`
的 `action_timeout_sec`）。**QQ 上传几百 MB 的视频要几十秒**，10 秒必然超时，
适配器会判定发送失败并断开重连，整条推文就丢了。所以要用大视频就得改：

```toml
[luma_client]
action_timeout_sec = 180.0
```

实测 298MB 视频从「发出请求」到「SnowLuma 返回成功」约 **39 秒**，设 180 秒有余量。
`media.max_video_mb` 要和它对得上：超时时间内传不完的大小，设了也发不出去。

### 大视频不会"发完就删"

容器里的视频文件由 `media.video_keep_seconds`（默认 180 秒）控制保留时间：
适配器拿到的是容器内路径，NapCat 可能在 `send` 返回之后还在读这个文件，
删太早 QQ 那边会收到失败或损坏的视频。本地临时文件始终是发完就删。
插件启动时还会扫掉容器里超过「保留时长 ×2（至少 10 分钟）」的遗留文件
（插件重载会取消挂着的延迟删除任务）。

### 找不到 docker 命令怎么办

Ubuntu 上用 snap 装的 docker，命令只在 `/snap/bin/docker`，而 **systemd 服务的 `PATH` 默认不含
`/snap/bin`**。插件会先查 `PATH`，再依次兜底 `/snap/bin/docker`、`/usr/bin/docker`、
`/usr/local/bin/docker`，启动时还会打一行自检：

```text
视频大文件通道就绪：docker=/snap/bin/docker 容器=snowluma 内联上限=10.0MB 外链上限=300MB
```

如果这行变成 `找不到 docker 可执行文件（PATH=…）`，超限视频就只会发封面。两种修法：

1. 给 systemd unit 的 `PATH` 补上 `/snap/bin`（推荐）：

   ```ini
   Environment=PATH=/home/<user>/.local/bin:/snap/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
   ```

   改完 `sudo systemctl daemon-reload && sudo systemctl restart maibot`。
2. 或者不用 snap 版 docker，装一个 `/usr/bin/docker`。

拷贝视频进容器时先试 `docker cp`；失败（snap 版 docker 有**私有 `/tmp`**，
宿主 `/tmp` 里的文件它 `lstat` 不到；家目录里的隐藏路径也读不到）会自动改成
`docker exec -i … cat > 容器内路径` 把字节流灌进去。每一步失败都会写日志，不会静默降级。

### 视频相关日志

| 日志 | 含义 |
| --- | --- |
| `聊天记录已发出，其中 1 个视频节点：<推文ID>=容器路径` | 视频真的发出去了（默认走聊天记录节点） |
| `已内联发送视频 <url>` / `已通过容器路径发送视频 <url>` | 视频单独成条时的成功日志 |
| `视频没能随推文发出，这条改为封面 + 链接: <ID>` | 视频没发出去，只发了封面（上一行会说明原因） |
| `找不到 docker 可执行文件（PATH=…）` | 见上面「找不到 docker 命令怎么办」 |
| `视频超过上限 300MB，没有可用变体，只发封面: <ID>` | 调大 `media.max_video_mb` 或接受封面 |

## 🌐 自动翻译

推文正文（以及「引用」内容）会自动翻译成目标语言，**译文直接替换原文**，
推文里不会同时出现原文和译文。

- **模型**：默认用 `replyer` 任务，也就是 `model_config.toml` 里 `[model_task_config.replyer]`
  配的那个模型；也可以换成 `utils`（更快更省）等任意任务名。
- **跳过**：本来就是目标语言的推文不翻（优先看接口返回的 `lang`，拿不到时按字符比例兜底）；
  超过 `translation.max_chars` 的超长推文也不翻，省 token。
- **缓存**：译文按「推文 ID + 目标语言」缓存，同一条推文推到多个群不会重复调用模型。
- **失败不影响推送**：超时、模型报错、返回空，都会静默保留原文照常发出。

长文本（例如 Steam 公告正文）会**先翻全文再截断**，并按 `link.translate_chunk_chars`
分段翻译，避免一次输出撞上模型 `max_tokens` 被截掉后半段。

## 🔗 链接内容

推文里带链接时，插件会把链接那头的正文抓出来贴在推文下面，并按 `[link]` 的设置一起翻译。

| 链接类型 | 做法 |
| --- | --- |
| Steam 新闻（`store.steampowered.com/news/app/<appid>/view/<gid>`） | 走 Steam 官方 RSS 按公告 ID 精确匹配（页面正文是 JS 渲染的，直接抓只能抓到导航栏） |
| 其它网页 | `trafilatura → readability → og:description` 逐级抽正文，并过滤导航/页脚垃圾 |

- **连不上会走代理**：默认先直连，抓不到就自动用 `[twitter] proxy` 重试一次；
  也可以给 `[link] proxy` 单独指定。
- 抓到的正文按 URL 缓存，同一条链接不会重复抓取与翻译。
- 抓取失败只是**不加这个块**，推文照常推送。

## 🧩 给 LLM 的工具

插件注册了一个只读工具 `twitter_latest(handle, count)`，AI 可以直接查某个推主的最新推文，
用来回答"某某最近发了什么"。

## ❓ 常见问题

| 现象 | 处理 |
| --- | --- |
| 提示「共 N 个字符，超过 X 用户名的 15 字符上限」 | 名字打错了。X 用户名最多 15 字符，插件会顺便猜一个真实存在的名字，例如 `limbus_company_b` 会提示 `LimbusCompany_B` |
| 只有文字没有图 | 检查 `twitter.proxy` 是否可用：`curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' https://pbs.twimg.com/media/xxx.jpg` |
| 视频只来了封面 | 先看启动日志那行 `视频大文件通道就绪：docker=…`：若为 `找不到 docker 可执行文件（PATH=…）`，见「找不到 docker 命令怎么办」；其次看 `视频没能随推文发出` 上一行写的原因（超 `max_video_mb` / 下载失败 / 拷贝失败） |
| 视频下载了但发不出去 | 日志里若有 `聊天记录发送失败` + 适配器的 `SnowLuma action 等待响应超时: action=send_private_forward_msg`，就是适配器 10 秒超时被大视频上传撞穿了，见「关键：把适配器的 action 超时调大」 |
| 一直提示拉取失败 | 看 `logs/app_*.log.jsonl` 里 `plugin.polarbear.twitter-forwarder` 的记录；私密账号会一直失败，属正常 |
| 命令没反应 | 确认插件已加载（`/tw_status`）。若是 🔒 跨聊天命令，检查 `command.cross_chat_admin_only` 与 `command.admins`，或 `command.admin_only` 是否把你自己挡在外面 |
| 想调整推送内容 | 改 `[display]` 段：作者行、链接、互动数据、正文截断长度都可以单独关 |
| 一次收到太多条 | 调小 `poll.max_tweets_per_poll`，或调大 `poll.interval_minutes` |
| 链接内容块不出现 | 先看日志有没有 `链接内容跳过（SSRF 防护）`：目标是内网地址时会被主动丢弃（见[安全说明](#-安全说明)）；没有这条日志则多半是页面抓不到正文 |

## 🛠 开发与自测

`tests/` 里带了可以直接跑的脚本，用 MaiBot 自带的 venv 执行（需要 `maibot_sdk` 和 `aiohttp`）：

```bash
cd plugins/<插件目录>/tests

# 389 项检查：解析、命令正则、轮询去重、投递回退、视频路由、翻译、链接、权限、SSRF
~/maimai/MaiBot/.venv/bin/python test_plugin.py

# 真实网络预览：看一条推文会被渲染成什么样
~/maimai/MaiBot/.venv/bin/python preview.py elonmusk

# 真机验证视频容器借道：真实视频下载 → 拷进 SnowLuma → 容器内校验 → 清理
~/maimai/MaiBot/.venv/bin/python check_docker_route.py
```

`test_plugin.py` 会真的请求 FxTwitter 并下载图片，需要一个可用代理；
`check_docker_route.py` 需要 docker 权限。

> 可选增强（`trafilatura` / `beautifulsoup4` / `readability-lxml`）不在 `_manifest.json`
> 的 `dependencies` 里，也不在主程序依赖基线中：它们**只影响链接正文的抽取质量**，
> 缺失时插件会退化成正则清洗 + `og:description`，不会因此加载失败——
> 这也避免了在别人机器上因为装不上重依赖而把插件整个卡住。
> 想启用就手动装到 MaiBot 的 venv：
> `~/maimai/MaiBot/.venv/bin/python -m pip install trafilatura beautifulsoup4 readability-lxml`，
> 启动日志的 `链接正文提取器：…` 会显示实际可用的提取器。

### 目录结构

```text
.
├── _manifest.json      # 插件元信息（manifest v2）
├── plugin.py           # 插件主体（单文件）
├── CHANGELOG.md        # 更新日志
├── LICENSE             # GPL-3.0-or-later
└── tests/
    ├── test_plugin.py        # 离线 + 在线自测
    ├── preview.py            # 渲染预览
    └── check_docker_route.py # 视频容器借道真机验证
```

> `config.toml` / `state.json` 都是**实例文件**，由 Runner 生成和维护，不在本仓库里。

## 📄 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 📜 许可证

[GPL-3.0-or-later](LICENSE) © polarbear
