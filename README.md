# astrbot_plugin_wife · 娶老婆

在群里发一个「娶」，随机抽一位群友当今天的对象：**对方头像 + “您今天的对象是 群昵称 (QQ号)”**。
不满意可以再说一句「换一个」。

## 功能

- `娶`：在本群随机抽一位群友当今天的对象。
- `换一个`：重新抽一位，会排除本轮已经抽过的人；群里的人都抽过一轮后会提示「没有别的群友可以换了」。
- 回复内容：对方头像 + 文案，可选是否 @ 对方。
- 可选「只抽最近 N 天发过言的群友」，避免抽到长期潜水的群友。
- 可选「N 分钟后撤回」，1-60 分钟；超过 2 分钟要求机器人在该群是管理员。
- 群白名单 / 群黑名单，两个名单都留空时不启用过滤。
- 命中「娶」/「换一个」后会调用 `event.stop_event()`，跳过其它插件与默认的 LLM 回复，不会出现「抽完又聊天」的情况。

## 安装

仓库：<https://github.com/Luckyumimi/astrbot_plugin_wife>

**方式一：从链接安装（推荐）**

AstrBot WebUI → 插件 → 安装插件 → **从链接安装**，填入仓库地址：

```
https://github.com/Luckyumimi/astrbot_plugin_wife
```

**方式二：手动放目录**

```bash
cd AstrBot/data/plugins
git clone https://github.com/Luckyumimi/astrbot_plugin_wife.git
# 重启 AstrBot
```

依赖：无（`requirements.txt` 为空，不需要额外 `pip install`）。平台：`aiocqhttp`（OneBot 11：NapCat / LLOneBot / Lagrange 等）。

首次安装后到「插件」页找到「娶老婆」，确认配置后重启或重载插件即可。

## 指令

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `娶` | 随机抽一位群友当今天的对象 | 所有人 |
| `换一个` | 换一位对象 | 所有人 |

两条指令都注册了 `CommandFilter`，所以 [astrbot_plugin_help](https://github.com/tinkerbellqwq/astrbot_plugin_help)
的帮助图里能直接看到它们。同时为了让「不加唤醒前缀、不 @ 机器人」也能触发，
额外注册了同名的正则处理器（`^娶$` / `^换一个$`），两者靠事件去重，只会回复一次。

> 唤醒规则：`/娶`、`@机器人 娶` 与裸 `娶` 都可以；只认**整条消息只有一个「娶」**，
> 带其它内容（例如「娶老婆」）不会触发。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `message_template` | string | `“您今天的对象是 {name} ({qq})”` | 文案模板，支持 `{name}`、`{qq}`、`{group}` |
| `send_avatar` | bool | `true` | 是否发送对方头像（`q1.qlogo.cn`） |
| `mention_target` | bool | `true` | 抽取时是否 @ 对方 |
| `recall_minutes` | int | `0` | 多少分钟后撤回，`0` = 不撤回，可填 1-60 |
| `admin_groups` | list | `[]` | 机器人是管理员的群号；这些群允许撤回时间超过 2 分钟 |
| `auto_detect_bot_admin` | bool | `true` | 没写在 `admin_groups` 里的群，是否调用 `get_group_member_info` 自动判断机器人身份 |
| `activity_days` | int | `0` | 只抽最近 N 天发过言的群友，`0` = 不限制 |
| `activity_max_pages` | int | `10` | 拉取群历史消息的最大页数（每页 100 条） |
| `exclude_requester` | bool | `true` | 不抽发指令的人自己 |
| `exclude_bots` | bool | `true` | 不抽群里的其它机器人（需要协议端提供 `is_robot`） |
| `whitelist_groups` | list | `[]` | 群白名单，留空 = 不启用 |
| `blacklist_groups` | list | `[]` | 群黑名单，留空 = 不启用，优先级高于白名单 |
| `recall_previous_on_change` | bool | `true` | 「换一个」时立即撤回上一条结果 |

配置文件位置：`AstrBot/data/config/astrbot_plugin_wife_config.json`，也可以在 WebUI 里改。
抽取记录保存在 `AstrBot/data/plugin_data/astrbot_plugin_wife/state.json`，保留 7 天，
用于「换一个」排除已抽过的人和重启后撤回上一条消息。

## 实现说明

- **撤回为什么需要 message_id**：AstrBot 的 `event.send()` 不返回平台消息 ID，
  所以插件在 aiocqhttp 平台上直接调用 `send_group_msg` 拿到 `message_id`，再用
  `delete_msg` 定时撤回；其它平台退回 `event.send()`，不撤回（会打日志）。
- **2 分钟限制**：QQ 群管理员才能撤回超过 2 分钟的群消息。`recall_minutes > 2` 且
  机器人不是管理员时，插件会按 2 分钟撤回并打一条 warning；把这些群写进
  `admin_groups` 可以跳过自动检测。
- **活跃筛选**：依赖协议端的 `get_group_msg_history`（NapCat / LLOneBot 支持），
  按页往更早翻直到超出时间窗口。接口不支持时会打 warning 并忽略该限制，
  而不是直接抽不出来。
- **只认整条消息**：命令处理器与正则处理器都要求 `message_str.strip()` 恰好等于
  `娶` / `换一个`，所以「娶老婆」「娶 谁」这类消息不会触发。

## 许可

MIT
