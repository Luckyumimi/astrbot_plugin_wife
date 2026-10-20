"""发「娶」随机抽一位群友当今天的对象。

功能
----
- ``娶``：从本群成员中随机抽一位，回复「对方头像 + “您今天的对象是 群昵称 (QQ)”」。
- ``换一个``：在本群重新抽一位，排除本轮已经抽过的人。
- 可选：只抽最近 N 天发过言的群友、1~60 分钟后撤回、抽完是否 @ 对方、群白名单 / 黑名单。

实现要点
--------
1. ``娶`` / ``换一个`` 同时注册了 ``CommandFilter`` 与 ``RegexFilter``：
   ``CommandFilter`` 让 astrbot_plugin_help 能扫描到指令（它只认 CommandFilter /
   CommandGroupFilter），而 ``CommandFilter`` 要求 ``event.is_at_or_wake_command``
   为真（即 @ 机器人、带唤醒前缀或免唤醒配置），所以再加一个正则 handler 兜底裸消息。
   两个 handler 共用 ``_dispatch``，靠 event extra 去重，只会回复一次。
2. 命中后调用 ``event.stop_event()``，跳过其余插件与默认的 LLM 回复。
3. 撤回需要 message_id，而 AstrBot 的 ``event.send()`` 不返回它。aiocqhttp 平台直接调用
   ``send_group_msg`` 拿到 message_id；其它平台退化为 ``event.send()``，此时不撤回。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star

try:  # StarTools 是较新版本才有的工具类，缺失时用 __file__ 推导数据目录
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - 仅老版本 AstrBot 会走到
    StarTools = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_wife"
PICK_COMMAND = "娶"
CHANGE_COMMAND = "换一个"
AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

ADMIN_RECALL_LIMIT_MINUTES = 2
"""非群管理员最多只能撤回 2 分钟内的消息。"""
MAX_RECALL_MINUTES = 60

MEMBER_CACHE_SECONDS = 60
SPEAKER_CACHE_SECONDS = 120
ADMIN_CACHE_SECONDS = 600
STATE_TTL_SECONDS = 7 * 86400
STATE_FILE_NAME = "state.json"
HISTORY_PAGE_SIZE = 100

DEFAULTS: dict[str, Any] = {
    "activity_days": 0,
    "activity_max_pages": 10,
    "recall_minutes": 0,
    "admin_groups": [],
    "auto_detect_bot_admin": True,
    "mention_target": True,
    "send_avatar": True,
    "message_template": "“您今天的对象是 {name} ({qq})”",
    "exclude_requester": True,
    "exclude_bots": True,
    "whitelist_groups": [],
    "blacklist_groups": [],
    "recall_previous_on_change": True,
}


def _as_int(value: Any, default: int) -> int:
    """把配置值/接口值转成整数，失败时返回默认值。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_id(value: Any) -> int | str:
    """QQ 号、群号、消息 id 在多数协议端是整数，转不过去就原样传。"""
    text = str(value).strip()
    return int(text) if text.isdigit() else text


def _as_str_list(value: Any) -> set[str]:
    """配置里的 list 可能混有 int，统一成字符串集合。"""
    if not isinstance(value, (list, tuple, set)):
        return set()
    result: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text:
            result.add(text)
    return result


@dataclass(frozen=True)
class Member:
    user_id: str
    name: str
    is_bot: bool = False


@dataclass
class PickState:
    """某个群、某个人最近一次的抽取结果。"""

    user_id: str
    name: str
    picked: list[str] = field(default_factory=list)
    message_id: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "picked": list(self.picked),
            "message_id": self.message_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> PickState | None:
        if not isinstance(raw, dict):
            return None
        user_id = str(raw.get("user_id") or "").strip()
        if not user_id:
            return None
        picked = raw.get("picked")
        return cls(
            user_id=user_id,
            name=str(raw.get("name") or user_id),
            picked=[str(i) for i in picked] if isinstance(picked, list) else [],
            message_id=str(raw.get("message_id") or ""),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


class PickStore:
    """把抽取结果落盘，让「换一个」在重启后仍能排除已抽过的人、撤回上一次的消息。"""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._data: dict[str, PickState] = {}

    @staticmethod
    def _key(group_id: str, sender_id: str) -> str:
        return f"{group_id}:{sender_id}"

    def load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # 文件损坏不应该影响插件启动
            logger.warning(f"[{PLUGIN_NAME}] 读取状态文件失败，将忽略旧数据：{exc}")
            return
        picks = raw.get("picks") if isinstance(raw, dict) else None
        if not isinstance(picks, dict):
            return
        now = time.time()
        for key, item in picks.items():
            state = PickState.from_dict(item)
            if state and now - state.updated_at <= STATE_TTL_SECONDS:
                self._data[str(key)] = state

    def get(self, group_id: str, sender_id: str) -> PickState | None:
        return self._data.get(self._key(group_id, sender_id))

    def put(self, group_id: str, sender_id: str, state: PickState) -> None:
        self._data[self._key(group_id, sender_id)] = state
        self.save()

    def prune(self) -> None:
        now = time.time()
        self._data = {
            key: state
            for key, state in self._data.items()
            if now - state.updated_at <= STATE_TTL_SECONDS
        }

    def save(self) -> None:
        if not self._path:
            return
        self.prune()
        payload = {
            "version": 1,
            "picks": {key: state.to_dict() for key, state in self._data.items()},
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存状态文件失败：{exc}")


class WifePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        # 只把 context 交给父类：不同 AstrBot 版本的 Star.__init__ 签名不完全一致，
        # 而父类本身也不使用 config。
        super().__init__(context)
        self.config: dict = config or {}
        self._store = PickStore(self._resolve_state_path())
        self._store.load()
        self._recall_tasks: dict[str, asyncio.Task] = {}
        self._member_cache: dict[str, tuple[float, list[Member]]] = {}
        self._speaker_cache: dict[str, tuple[float, int, set[str]]] = {}
        self._admin_cache: dict[str, tuple[float, bool]] = {}
        self._clamp_warned: set[str] = set()

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str) -> Any:
        value = self.config.get(key) if hasattr(self.config, "get") else None
        if value is None:
            return DEFAULTS[key]
        return value

    def _cfg_bool(self, key: str) -> bool:
        value = self._cfg(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, *, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, _as_int(self._cfg(key), int(DEFAULTS[key]))))

    def _tpl_text(self, name: str, qq: str, group_id: str) -> str:
        template = str(self._cfg("message_template") or "").strip()
        if not template:
            template = str(DEFAULTS["message_template"])
        return (
            template.replace("{name}", name)
            .replace("{qq}", qq)
            .replace("{group}", group_id)
        )

    def _group_allowed(self, group_id: str) -> bool:
        whitelist = _as_str_list(self._cfg("whitelist_groups"))
        blacklist = _as_str_list(self._cfg("blacklist_groups"))
        if whitelist and group_id not in whitelist:
            return False
        return group_id not in blacklist

    @staticmethod
    def _resolve_state_path() -> Path | None:
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(PLUGIN_NAME)) / STATE_FILE_NAME
            except Exception as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 无法通过 StarTools 获取数据目录：{exc}",
                )
        # 兜底：插件位于 <AstrBot>/data/plugins/<插件目录>/，数据目录是 <AstrBot>/data/plugin_data/<插件名>/
        try:
            data_dir = Path(__file__).resolve().parents[2] / "plugin_data" / PLUGIN_NAME
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir / STATE_FILE_NAME
        except Exception as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] 无法创建数据目录，抽取记录将不会持久化：{exc}",
            )
            return None

    # -------------------------------------------------------------- 平台调用

    @staticmethod
    async def _call_action(
        bot: Any,
        self_id: str,
        action: str,
        **params: Any,
    ) -> Any:
        """调用 OneBot 11 API；self_id 用于多账号时路由，与 AstrBot 内部一致。"""
        if self_id:
            params.setdefault("self_id", self_id)
        return await bot.call_action(action, **params)

    async def _fetch_members(
        self,
        bot: Any,
        self_id: str,
        group_id: str,
    ) -> list[Member] | None:
        now = time.time()
        cached = self._member_cache.get(group_id)
        if cached and cached[0] > now:
            return cached[1]

        try:
            raw = await self._call_action(
                bot,
                self_id,
                "get_group_member_list",
                group_id=_as_id(group_id),
            )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 获取 {group_id} 群成员失败：{exc}")
            return None
        if not isinstance(raw, list):
            logger.warning(
                f"[{PLUGIN_NAME}] 获取 {group_id} 群成员的返回格式异常：{type(raw).__name__}",
            )
            return None

        members: list[Member] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            user_id = item.get("user_id")
            if user_id is None:
                continue
            name = str(item.get("card") or item.get("nickname") or user_id).strip()
            members.append(
                Member(
                    user_id=str(user_id),
                    name=name or str(user_id),
                    is_bot=bool(item.get("is_robot")),
                ),
            )
        self._member_cache[group_id] = (now + MEMBER_CACHE_SECONDS, members)
        return members

    async def _history_page(
        self,
        bot: Any,
        self_id: str,
        group_id: str,
        cursor: Any,
    ) -> Any:
        """取一页群历史消息。不同协议端对 message_seq 的默认值要求不一致，第一页多试一次。"""
        base: dict[str, Any] = {
            "group_id": _as_id(group_id),
            "count": HISTORY_PAGE_SIZE,
        }
        if cursor is None:
            variants = [base, {**base, "message_seq": 0}]
        else:
            variants = [{**base, "message_seq": cursor, "reverseOrder": True}]

        last_error: Exception | None = None
        for params in variants:
            try:
                return await self._call_action(
                    bot, self_id, "get_group_msg_history", **params
                )
            except Exception as exc:  # 换一种参数再试
                last_error = exc
        raise last_error if last_error else RuntimeError("get_group_msg_history failed")

    async def _recent_speakers(
        self,
        bot: Any,
        self_id: str,
        group_id: str,
        days: int,
    ) -> set[str] | None:
        """返回最近 days 天在本群发过言的人；协议端不支持历史消息时返回 None。"""
        now = time.time()
        cached = self._speaker_cache.get(group_id)
        if cached and cached[0] > now and cached[1] >= days:
            return cached[2]

        deadline = now - days * 86400
        speakers: set[str] = set()
        cursor: Any = None
        pages = self._cfg_int("activity_max_pages", minimum=1, maximum=50)

        for page in range(pages):
            try:
                ret = await self._history_page(bot, self_id, group_id, cursor)
            except Exception as exc:
                if page == 0:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 协议端不支持 get_group_msg_history，"
                        f"忽略「最近 {days} 天发过言」限制：{exc}",
                    )
                    return None
                break

            messages = ret.get("messages") if isinstance(ret, dict) else ret
            if not isinstance(messages, list) or not messages:
                break

            oldest_time: float | None = None
            oldest_seq: Any = None
            for message in messages:
                if not isinstance(message, dict):
                    continue
                stamp = message.get("time")
                stamp_float = float(stamp) if isinstance(stamp, (int, float)) else None
                # 翻页可能带回窗口外的旧消息：只统计窗口内发言的人，但用页内最早时间决定是否继续翻页
                if stamp_float is None or stamp_float >= deadline:
                    sender = message.get("sender")
                    if isinstance(sender, dict) and sender.get("user_id") is not None:
                        speakers.add(str(sender["user_id"]))
                if stamp_float is None:
                    continue
                if oldest_time is None or stamp_float < oldest_time:
                    oldest_time = stamp_float
                    oldest_seq = (
                        message.get("message_seq")
                        or message.get("real_seq")
                        or message.get("message_id")
                    )

            if oldest_time is None or oldest_time <= deadline:
                break
            if oldest_seq is None or str(oldest_seq) == str(cursor):
                break
            cursor = oldest_seq

        self._speaker_cache[group_id] = (now + SPEAKER_CACHE_SECONDS, days, speakers)
        return speakers

    async def _bot_is_group_admin(self, bot: Any, self_id: str, group_id: str) -> bool:
        if group_id in _as_str_list(self._cfg("admin_groups")):
            return True
        if not self._cfg_bool("auto_detect_bot_admin"):
            return False

        now = time.time()
        cached = self._admin_cache.get(group_id)
        if cached and cached[0] > now:
            return cached[1]

        is_admin = False
        try:
            info = await self._call_action(
                bot,
                self_id,
                "get_group_member_info",
                group_id=_as_id(group_id),
                user_id=_as_id(self_id or "0"),
            )
            role = str((info or {}).get("role") or "") if isinstance(info, dict) else ""
            is_admin = role in {"admin", "owner"}
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 查询机器人群权限失败：{exc}")

        self._admin_cache[group_id] = (now + ADMIN_CACHE_SECONDS, is_admin)
        return is_admin

    async def _effective_recall_minutes(
        self,
        bot: Any,
        self_id: str,
        group_id: str,
    ) -> int:
        minutes = self._cfg_int(
            "recall_minutes",
            minimum=0,
            maximum=MAX_RECALL_MINUTES,
        )
        if minutes <= ADMIN_RECALL_LIMIT_MINUTES:
            return minutes
        if await self._bot_is_group_admin(bot, self_id, group_id):
            return minutes
        if group_id not in self._clamp_warned:
            self._clamp_warned.add(group_id)
            logger.warning(
                f"[{PLUGIN_NAME}] 群 {group_id} 里机器人不是管理员，"
                f"撤回时间被限制为 {ADMIN_RECALL_LIMIT_MINUTES} 分钟"
                f"（配置值 {minutes} 分钟）。可在 admin_groups 中显式声明该群。",
            )
        return ADMIN_RECALL_LIMIT_MINUTES

    async def _send_plain(self, event: AstrMessageEvent, text: str) -> None:
        if not text:
            return
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 发送提示消息失败：{exc}")

    async def _send_pick(
        self,
        event: AstrMessageEvent,
        target: Member,
        text: str,
    ) -> str | None:
        """发送抽取结果，尽量返回 message_id（用于撤回）。"""
        mention = self._cfg_bool("mention_target")
        with_avatar = self._cfg_bool("send_avatar")
        avatar_url = AVATAR_URL_TEMPLATE.format(qq=target.user_id)

        chain: list[Any] = []
        if mention:
            chain.append(At(qq=target.user_id, name=target.name))
        if with_avatar:
            chain.append(Image.fromURL(avatar_url))
        chain.append(Plain(text))

        bot = getattr(event, "bot", None)
        group_id = str(event.get_group_id() or "")
        if bot is not None and group_id.isdigit():
            segments: list[dict[str, Any]] = []
            if mention:
                segments.append({"type": "at", "data": {"qq": target.user_id}})
            if with_avatar:
                segments.append({"type": "image", "data": {"file": avatar_url}})
            segments.append({"type": "text", "data": {"text": text}})
            try:
                ret = await self._call_action(
                    bot,
                    str(event.get_self_id() or ""),
                    "send_group_msg",
                    group_id=int(group_id),
                    message=segments,
                )
            except Exception as exc:
                logger.warning(
                    f"[{PLUGIN_NAME}] 直接发送失败，回退到 event.send()：{exc}",
                )
            else:
                if isinstance(ret, dict) and ret.get("message_id") is not None:
                    return str(ret["message_id"])
                return None

        await event.send(MessageChain(chain))
        return None

    # ------------------------------------------------------------------ 撤回

    def _schedule_recall(
        self,
        bot: Any,
        self_id: str,
        message_id: str,
        minutes: int,
        group_id: str,
    ) -> None:
        if minutes <= 0 or not message_id or bot is None:
            return
        self._cancel_recall(message_id)
        task = asyncio.create_task(
            self._recall_later(bot, self_id, message_id, minutes, group_id),
        )
        self._recall_tasks[str(message_id)] = task
        task.add_done_callback(
            lambda _task, key=str(message_id): self._recall_tasks.pop(key, None),
        )

    def _cancel_recall(self, message_id: str) -> None:
        task = self._recall_tasks.pop(str(message_id), None)
        if task and not task.done():
            task.cancel()

    async def _recall_later(
        self,
        bot: Any,
        self_id: str,
        message_id: str,
        minutes: int,
        group_id: str,
    ) -> None:
        try:
            await asyncio.sleep(minutes * 60)
            await self._delete_message(bot, self_id, message_id)
            logger.info(
                f"[{PLUGIN_NAME}] 已撤回群 {group_id} 的消息 {message_id}（{minutes} 分钟后）",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] 撤回群 {group_id} 的消息 {message_id} 失败：{exc}",
            )

    async def _delete_message(self, bot: Any, self_id: str, message_id: str) -> None:
        await self._call_action(
            bot,
            self_id,
            "delete_msg",
            message_id=_as_id(message_id),
        )

    # -------------------------------------------------------------- 抽取逻辑

    def _build_pool(
        self,
        members: list[Member],
        self_id: str,
        sender_id: str,
    ) -> list[Member]:
        exclude_requester = self._cfg_bool("exclude_requester")
        exclude_bots = self._cfg_bool("exclude_bots")
        pool: list[Member] = []
        for member in members:
            if self_id and member.user_id == self_id:
                continue
            if exclude_requester and member.user_id == sender_id:
                continue
            if exclude_bots and member.is_bot:
                continue
            pool.append(member)
        return pool

    async def _dispatch(self, event: AstrMessageEvent, *, change: bool) -> None:
        command = CHANGE_COMMAND if change else PICK_COMMAND
        if event.get_message_str().strip() != command:
            # 只认单条「娶」/「换一个」，带参数的交给别人
            return

        group_id = str(event.get_group_id() or "")
        if not group_id or not self._group_allowed(group_id):
            return

        dedupe_key = f"{PLUGIN_NAME}:{command}"
        if event.get_extra(dedupe_key):
            return
        event.set_extra(dedupe_key, True)

        try:
            await self._act(event, change=change)
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 抽取失败：{exc}", exc_info=True)
            await self._send_plain(event, "抽老婆出错了，等会儿再试试。")
        finally:
            # 命中「娶」就不再让其它插件和 LLM 处理
            event.stop_event()

    async def _act(self, event: AstrMessageEvent, *, change: bool) -> None:
        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        bot = getattr(event, "bot", None)
        if bot is None:
            await self._send_plain(event, "这个指令需要 aiocqhttp（OneBot 11）适配器。")
            return

        state = self._store.get(group_id, sender_id)

        members = await self._fetch_members(bot, self_id, group_id)
        if members is None:
            await self._send_plain(event, "获取群成员列表失败了，等会儿再试试。")
            return

        pool = self._build_pool(members, self_id, sender_id)
        if not pool:
            await self._send_plain(event, "群里没有可以抽的人……")
            return

        days = self._cfg_int("activity_days", minimum=0, maximum=90)
        if days > 0:
            speakers = await self._recent_speakers(bot, self_id, group_id, days)
            if speakers is not None:
                active = [m for m in pool if m.user_id in speakers]
                if not active:
                    await self._send_plain(
                        event,
                        f"最近 {days} 天没看到有人在群里说话，抽不出对象。",
                    )
                    return
                pool = active

        already_picked = list(state.picked) if (change and state) else []
        picked_set = set(already_picked)
        candidates = [m for m in pool if m.user_id not in picked_set]
        if not candidates:
            if change:
                await self._send_plain(
                    event,
                    "群里已经没有别的群友可以换了，这一个先留着吧。",
                )
                return
            candidates = pool
            already_picked = []

        target = random.choice(candidates)

        text = self._tpl_text(target.name, target.user_id, group_id)
        message_id = await self._send_pick(event, target, text)

        minutes = await self._effective_recall_minutes(bot, self_id, group_id)
        previous_message_id = state.message_id if state else ""
        picked = [*already_picked, target.user_id] if change else [target.user_id]
        self._store.put(
            group_id,
            sender_id,
            PickState(
                user_id=target.user_id,
                name=target.name,
                picked=picked,
                message_id=message_id or "",
                updated_at=time.time(),
            ),
        )

        if change and previous_message_id and self._cfg_bool("recall_previous_on_change"):
            self._cancel_recall(previous_message_id)
            try:
                await self._delete_message(bot, self_id, previous_message_id)
            except Exception as exc:
                logger.debug(
                    f"[{PLUGIN_NAME}] 撤回上一条抽取消息失败（可能是 ID 已过期）：{exc}",
                )

        self._schedule_recall(bot, self_id, message_id or "", minutes, group_id)
        if minutes > 0 and not message_id:
            logger.info(
                f"[{PLUGIN_NAME}] 群 {group_id} 未取得消息 ID，本次结果不会撤回"
                f"（平台可能不支持 send_group_msg 返回 message_id）",
            )

    # ---------------------------------------------------------------- 指令注册

    @filter.command(PICK_COMMAND, priority=0)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def pick_wife(self, event: AstrMessageEvent) -> None:
        """随机抽取一位群友当今天的对象"""
        await self._dispatch(event, change=False)

    @filter.regex(rf"^{PICK_COMMAND}$", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def pick_wife_without_wake(self, event: AstrMessageEvent) -> None:
        """（免唤醒）直接发送 娶 也能抽一位群友当今天的对象"""
        await self._dispatch(event, change=False)

    @filter.command(CHANGE_COMMAND, priority=0)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def change_wife(self, event: AstrMessageEvent) -> None:
        """换一个今天的对象"""
        await self._dispatch(event, change=True)

    @filter.regex(rf"^{CHANGE_COMMAND}$", priority=1)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def change_wife_without_wake(self, event: AstrMessageEvent) -> None:
        """（免唤醒）直接发送 换一个 也能换一位对象"""
        await self._dispatch(event, change=True)

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self) -> None:
        for task in list(self._recall_tasks.values()):
            if not task.done():
                task.cancel()
        self._recall_tasks.clear()
        self._store.save()
