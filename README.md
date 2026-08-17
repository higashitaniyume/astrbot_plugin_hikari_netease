# astrbot_plugin_netease

网易云音乐解析插件：自动识别 `music.163.com` 歌曲/专辑/歌单/播客链接与 `163cn.tv` 短链接（含 QQ 分享卡片）。

## 功能

- **单曲 / 播客**：下载音频后发送
- **专辑 / 歌单**：批量下载后打包 ZIP 发送（仅私聊）
- **默认最高音质**：默认发 FLAC（高音质）；想换格式，**回复机器人的消息**发 `mp3` 或 `flac`，即按目标格式重发并记住偏好
- **手动解析**：群聊默认仅「@bot + 链接」或「@bot + 引用卡片」时解析；可在配置中指定 `auto_parse_groups` 让指定群自动解析
- **卡片引导**：群聊收到网易云链接但未 @ 时，回一句引导（带同群冷却）

## 用法

| 输入 | 效果 |
|------|------|
| 发送网易云歌曲链接（私聊） | 自动下载并发送 |
| 发送网易云卡片（私聊） | 自动解析并发送 |
| 发送专辑/歌单链接（私聊） | 打包 ZIP 发送 |
| 群聊 @机器人 + 链接 | 解析（专辑/歌单会提示私聊） |
| 群聊 @机器人 + 引用卡片 | 解析被引用的卡片 |
| 回复机器人的消息 + `mp3`/`flac` | 换格式重发并记住偏好 |

## 配置

- **必填**：`api_base_url` — api-enhanced 网易云 API 服务器地址（如 `http://192.168.1.10:3000`），可使用 [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) 或类似服务
- `cookie`：登录网易云账号的 cookie，可解析 VIP 歌曲完整音频（可选）
- `default_quality`：默认音质 `flac` / `mp3`
- `auto_parse_groups`：自动解析的群列表（默认群聊需 @ 触发）
- `card_hint`：群聊卡片引导提示开关与冷却

## 依赖

- `httpx>=0.27.0`（在 requirements.txt 中声明）

## 协议

AGPL-3.0-or-later
