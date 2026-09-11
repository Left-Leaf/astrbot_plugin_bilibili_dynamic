"""AstrBot B 站动态蹲饼插件。

功能概览：

1. **控制台独占的引擎开关**：蹲饼 / 模拟人格这类会操纵本机浏览器的能力
   **不注册为聊天指令**，只能在插件控制台页面（``pages/console``）下发，
   用户侧无法开启。
2. **蹲饼不筛选**：依赖库把关注流里**全部 UP** 的动态原样回传，筛选完全由
   插件负责（``/关注up`` 加入本群列表后才会分发该 UP 的动态）。
3. **群订阅 = 「群号 → 关注 UP 列表」**：群管理员在群里发送
   ``/关注up <uid>`` 把某个 UP 加入**本群**的关注列表（同时调用内核在运行态
   关注该 UP，让其动态进入关注流），``/取关up <uid>`` 移除，``/关注列表`` 查看。
   捕获到动态后，只分发给关注了该作者的群——同一个 UP 可以被多个群分别订阅，
   互不影响。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .bridge import BridgeProcess
from .card import render_share_card
from .processor import STYLE_AUTO, STYLE_RICH, STYLE_TEXT, build_dynamic_chain

PLUGIN_NAME = "astrbot_plugin_bilibili"
"""插件唯一识别名（Web API 路由与数据目录都用到）。"""

CONSOLE_HELP = """可用的控制台指令：
  start                启动引擎（拉起 Node 内核 + 浏览器，不开启任何功能）
  stop                 关闭引擎（结束浏览器与 Node 进程）
  login                确保登录（未登录时生成二维码，页面会显示）
  fetch on             开启蹲饼（捕获关注流全部动态，筛选由插件按群订阅完成）
  fetch off [close]    关闭蹲饼（带 close 时同时关闭动态页标签）
  follow <uid>         运行态主动关注一个 UP（独立操作，不进模拟任务流）
  card <动态id/链接>    把本次运行捕获过的动态渲染成分享图（存到插件数据目录的 cards/）
  sim on               开启模拟人格（养号任务流）
  sim off              结束模拟人格
  status               查看内核状态快照
  dynamics [n]         查看最近捕获的 n 条动态（默认 5）
  help                 查看内核自带指令列表"""
"""控制台页面展示的指令说明（与内核内置指令保持一致）。"""


@register(
    PLUGIN_NAME,
    "Left-Leaf",
    "B 站动态蹲饼：控制台控制引擎，按群订阅筛选分发动态",
    "2.1.0",
)
class BilibiliPlugin(Star):
    """B 站动态蹲饼插件（Node 桥接 + 控制台控制 + 群级 UP 订阅分发）。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """初始化插件：准备数据目录、订阅表、桥接进程与控制台 API。

        Args:
            context: AstrBot 上下文。
            config: 插件配置（对应 ``_conf_schema.json``）。
        """
        super().__init__(context)
        self.config = config or {}
        self.plugin_name = getattr(self, "name", None) or PLUGIN_NAME

        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name
        self.subscribers_file = self.data_dir / "subscribers.json"
        self.watermark_file = self.data_dir / "watermark.json"
        self.cards_dir = self.data_dir / "cards"
        self.node_dir = Path(__file__).parent / "node"

        self._subscribers: dict[str, dict[str, Any]] = {}
        self._load_subscribers()

        self.dispatch_logs: deque[dict] = deque(maxlen=30)
        """分发记录（新的在后）：控制台页面用它回答「蹲到了但为什么没发」。"""

        self._dynamic_watermark: float | None = self._load_watermark()
        """已见动态里最新的发布时间戳（新动态判定水位线）；None 表示从未获取过。"""

        self.bridge = BridgeProcess(
            self.node_dir,
            self.config,
            self._on_dynamics,
            self.data_dir,
        )

        self.context.register_web_api(
            f"/{PLUGIN_NAME}/console/state",
            self.api_console_state,
            ["GET"],
            "B 站蹲饼控制台状态",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/console/command",
            self.api_console_command,
            ["POST"],
            "执行 B 站蹲饼控制台指令",
        )

    async def initialize(self) -> None:
        """插件加载完成时的钩子（不自动拉起引擎，避免后台常驻浏览器）。"""
        total_targets = sum(
            len(entry.get("targets") or []) for entry in self._subscribers.values()
        )
        logger.info(
            f"[bilibili] 插件已加载：{len(self._subscribers)} 个群、{total_targets} 条 UP 订阅；"
            "引擎开关请在 WebUI 插件页面「控制台」中操作",
        )

    async def terminate(self) -> None:
        """插件卸载 / 重载时结束桥接进程，避免残留浏览器占用用户数据目录。"""
        await self.bridge.stop()

    # ===== 群订阅指令（用户侧，仅管理员、仅群聊） =====

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("关注up")
    async def cmd_follow_up(self, event: AstrMessageEvent, prompt: GreedyStr):
        """把某个 UP 加入本群关注列表，并在运行态关注该 UP。

        Args:
            event: 消息事件。
            prompt: 指令参数，``<uid 或空间链接>``。

        Yields:
            关注结果 + 本群关注列表。
        """
        uid = self._parse_up_uid(str(prompt))
        if not uid:
            yield event.plain_result(
                "用法：/关注up <uid 或空间链接>\n"
                "例如：/关注up 161775300\n"
                "（uid 是空间链接 space.bilibili.com/<uid> 里的数字）",
            )
            event.stop_event()
            return

        name = ""
        engine_note = ""
        if self.bridge.running:
            try:
                result = await self.bridge.follow_up(uid)
            except Exception as exc:  # noqa: BLE001 - 失败原因需要回显到聊天
                yield event.plain_result(f"❌ 关注 UP {uid} 失败：{exc}")
                event.stop_event()
                return
            if not result.get("ok"):
                yield event.plain_result(
                    f"❌ {result.get('output') or f'关注 {uid} 失败'}",
                )
                event.stop_event()
                return
            data = result.get("data") or {}
            uid = str(data.get("uid") or uid)
            name = str(data.get("name") or "").strip()
            engine_note = str(result.get("output") or "")
        else:
            engine_note = (
                "⚠️ 引擎未启动：订阅已记录，但还没真正关注该 UP"
                "（请在 WebUI 控制台 `start` 后重发一次本指令完成关注）"
            )

        entry = self._group_entry(event)
        targets = entry.setdefault("targets", [])
        existing = next((item for item in targets if item.get("uid") == uid), None)
        if existing is None:
            targets.append({"uid": uid, "name": name})
        elif name and existing.get("name") != name:
            existing["name"] = name
        entry["updated_at"] = int(time.time())
        self._save_subscribers()
        logger.info(
            f"[bilibili] 群 {entry.get('group_id')} 新增关注 UP {uid}（{name or '未命名'}）",
        )

        yield event.plain_result(
            "\n".join(
                [
                    engine_note or f"✅ 已关注 {name or uid}",
                    f"本群关注列表：{self._describe_targets(targets)}",
                ],
            ),
        )
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("取关up")
    async def cmd_unfollow_up(self, event: AstrMessageEvent, prompt: GreedyStr):
        """把某个 UP 移出本群关注列表（不会在 B 站上取关，其他群不受影响）。

        Args:
            event: 消息事件。
            prompt: 指令参数，``<uid 或空间链接>``。

        Yields:
            移除结果 + 本群关注列表。
        """
        uid = self._parse_up_uid(str(prompt))
        if not uid:
            yield event.plain_result("用法：/取关up <uid 或空间链接>")
            event.stop_event()
            return

        entry = self._group_entry(event)
        targets = entry.setdefault("targets", [])
        remaining = [item for item in targets if item.get("uid") != uid]
        if len(remaining) == len(targets):
            yield event.plain_result(
                f"ℹ️ 本群没有关注 UP {uid}。\n"
                f"本群关注列表：{self._describe_targets(targets)}",
            )
            event.stop_event()
            return

        entry["targets"] = remaining
        entry["updated_at"] = int(time.time())
        self._save_subscribers()
        logger.info(f"[bilibili] 群 {entry.get('group_id')} 移除关注 UP {uid}")

        yield event.plain_result(
            "\n".join(
                [
                    f"✅ 已把 {uid} 移出本群关注列表（B 站上未取关，其他群不受影响）",
                    f"本群关注列表：{self._describe_targets(remaining)}",
                ],
            ),
        )
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("关注列表")
    async def cmd_list_follows(self, event: AstrMessageEvent):
        """查看本群的 UP 关注列表。

        Args:
            event: 消息事件。

        Yields:
            本群关注列表。
        """
        entry = self._group_entry(event)
        targets = entry.get("targets") or []
        if not targets:
            yield event.plain_result(
                "本群还没有关注任何 UP。\n"
                "发送 `/关注up <uid 或空间链接>` 即可订阅（例如 `/关注up 161775300`）。",
            )
            event.stop_event()
            return

        lines = [f"本群关注列表（{len(targets)} 个 UP）："]
        for item in targets:
            uid = item.get("uid")
            name = item.get("name") or "（未知名称）"
            lines.append(f"· {name}（{uid}）")
        lines.append("用 `/取关up <uid>` 可移除。")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ===== 控制台 Web API（只有 WebUI 页面能调用） =====

    async def api_console_state(self):
        """返回控制台页面所需的全部状态。

        Returns:
            ``{"status": "ok", "data": {...}}`` 结构的 JSON 响应。
        """
        return json_response({"status": "ok", "data": self._console_state()})

    async def api_console_command(self):
        """执行一条控制台指令。

        Returns:
            ``{"status": "ok", "data": {"result": ..., "state": ...}}``；
            指令为空等错误场景返回错误响应。
        """
        payload = await request.json(default={})
        line = str(payload.get("line") or "").strip()
        if not line:
            return error_response("缺少指令内容")

        result = await self._run_console_command(line)
        return json_response(
            {
                "status": "ok",
                "data": {"result": result, "state": self._console_state()},
            },
        )

    async def _run_console_command(self, line: str) -> dict:
        """执行控制台指令（进程控制指令由 Python 处理，其余转发给 Node 内核）。

        Args:
            line: 完整指令文本，例如 ``start`` / ``fetch on``。

        Returns:
            形如 ``{"ok": bool, "output": str}`` 的执行结果。
        """
        head = line.split()[0].lower()
        try:
            if head == "start":
                if self.bridge.running:
                    return {"ok": True, "output": "引擎已在运行中。"}
                await self.bridge.start()
                return {"ok": True, "output": "✅ 引擎已启动（浏览器已打开）。"}
            if head == "stop":
                if not self.bridge.running:
                    return {"ok": True, "output": "引擎未在运行。"}
                await self.bridge.stop()
                return {"ok": True, "output": "🛑 引擎已关闭。"}
            if head == "help":
                return {"ok": True, "output": CONSOLE_HELP}
            if head == "card":
                # 把本次运行已捕获的动态渲染成分享图（不需要引擎，但需要已捕获的数据）
                dyn_id = "".join(
                    ch for ch in line.split(maxsplit=1)[-1] if ch.isdigit()
                )
                dynamic = next(
                    (
                        item
                        for item in self.bridge.dynamics
                        if str(item.get("dynId") or "") == dyn_id
                    ),
                    None,
                )
                if not dyn_id or dynamic is None:
                    return {
                        "ok": False,
                        "output": f"没找到动态 {dyn_id or '（缺少 id）'}：只能渲染本次运行已捕获的动态",
                    }
                card_path = await asyncio.to_thread(
                    render_share_card,
                    dynamic,
                    self.cards_dir / f"{dyn_id}.png",
                )
                return {"ok": True, "output": f"✅ 分享图已生成：{card_path}"}
            if not self.bridge.running:
                return {"ok": False, "output": "引擎未启动，请先执行 `start`。"}
            if head == "login":
                # 登录会一直阻塞到扫码完成，这里只下发不等待，二维码通过页面自动刷新展示
                return await self.bridge.execute(line, wait=False)
            return await self.bridge.execute(line)
        except Exception as exc:  # noqa: BLE001 - 指令失败需要回显给控制台
            logger.error(f"[bilibili] 控制台指令 `{line}` 执行失败: {exc}")
            return {"ok": False, "output": str(exc)}

    def _console_state(self) -> dict:
        """汇总控制台页面状态。

        Returns:
            引擎快照 + 群订阅列表 + 分发记录 + 指令帮助。
        """
        state = self.bridge.snapshot()
        state["subscribers"] = [
            {
                "umo": entry.get("umo", ""),
                "platform": entry.get("platform_name", ""),
                "group_id": entry.get("group_id", ""),
                "targets": [
                    {
                        "uid": item.get("uid", ""),
                        "name": item.get("name", ""),
                    }
                    for item in (entry.get("targets") or [])
                    if isinstance(item, dict)
                ],
                "updated_at": entry.get("updated_at", 0),
            }
            for entry in self._subscribers.values()
        ]
        state["subscribed_uids"] = self._subscribed_uids()
        state["dispatch_logs"] = list(self.dispatch_logs)
        state["help"] = CONSOLE_HELP
        return state

    # ===== 群订阅表 =====

    def _load_subscribers(self) -> None:
        """从数据目录读取「群 → 关注 UP 列表」（文件损坏时按空表处理）。

        兼容 v1 的旧订阅表（只有群信息、没有 UP 列表）：这类群迁移为空列表，
        等管理员重新用 ``/关注up`` 订阅。
        """
        self._subscribers = {}
        if not self.subscribers_file.exists():
            return
        try:
            data = json.loads(self.subscribers_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[bilibili] 订阅表读取失败，将重新开始: {exc}")
            return
        if not isinstance(data, dict):
            return

        migrated = 0
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            targets: list[dict] = []
            for item in value.get("targets") or []:
                if not isinstance(item, dict):
                    continue
                uid = str(item.get("uid") or "").strip()
                if uid and not any(existing["uid"] == uid for existing in targets):
                    targets.append(
                        {"uid": uid, "name": str(item.get("name") or "").strip()},
                    )
            if not targets and value.get("subscribed_at"):
                migrated += 1
            entry = dict(value)
            entry["targets"] = targets
            self._subscribers[str(key)] = entry

        if migrated:
            logger.info(
                f"[bilibili] 检测到 {migrated} 个旧版订阅群，已迁移为「群 → UP 列表」，"
                "需要管理员重新用 /关注up 指定要蹲的 UP",
            )

    def _save_subscribers(self) -> None:
        """把「群 → 关注 UP 列表」写回数据目录。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.subscribers_file.write_text(
            json.dumps(self._subscribers, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_watermark(self) -> float | None:
        """读取「上次已见动态的最新发布时间」（新动态判定水位线）。

        优先用插件自己存的 ``watermark.json``；没有时回退读依赖库的增量基线
        （``last-fetched-dynamic.json`` 里就是一个 ``pubTs``，即上次获取到的最新动态时间）。

        Returns:
            时间戳（秒）；两处都读不到时返回 None（表示从未获取过）。
        """
        sources = (
            self.watermark_file,
            self.node_dir
            / "node_modules"
            / "bilibili-user-simulation"
            / "data"
            / "last-fetched-dynamic.json",
        )
        for path in sources:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                value = float(data.get("pubTs") or 0)
            except (
                OSError,
                json.JSONDecodeError,
                AttributeError,
                TypeError,
                ValueError,
            ):
                continue
            if value > 0:
                return value
        return None

    def _save_watermark(self) -> None:
        """把水位线写回数据目录（重启后仍能区分新旧动态）。"""
        if self._dynamic_watermark is None:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.watermark_file.write_text(
            json.dumps(
                {"pubTs": self._dynamic_watermark, "at": int(time.time())},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _group_entry(self, event: AstrMessageEvent) -> dict:
        """取出（必要时创建）当前群的订阅记录。

        Args:
            event: 消息事件（群聊）。

        Returns:
            形如 ``{"umo": ..., "group_id": ..., "targets": [...]}`` 的订阅记录。
        """
        umo = event.unified_msg_origin
        entry = self._subscribers.get(umo)
        if entry is None:
            entry = {
                "umo": umo,
                "platform_id": event.get_platform_id(),
                "platform_name": event.get_platform_name(),
                "group_id": str(event.get_group_id() or event.get_session_id()),
                "targets": [],
                "updated_at": int(time.time()),
            }
            self._subscribers[umo] = entry
        return entry

    @staticmethod
    def _describe_targets(targets: list[dict]) -> str:
        """把关注列表格式化成一行文本（空列表给提示）。

        Args:
            targets: ``[{"uid": ..., "name": ...}]``。

        Returns:
            形如 ``明日方舟（161775300）、1265652806``；为空时返回「（空）」。
        """
        if not targets:
            return "（空，发送 /关注up <uid> 添加）"
        parts = []
        for item in targets:
            uid = str(item.get("uid") or "")
            name = str(item.get("name") or "").strip()
            parts.append(f"{name}（{uid}）" if name else uid)
        return "、".join(parts)

    def _subscribed_uids(self) -> list[dict]:
        """汇总所有群关注到的 UP（用于控制台展示）。

        Returns:
            ``[{"uid": ..., "name": ..., "groups": 群数}]``，按 uid 排序。
        """
        merged: dict[str, dict] = {}
        for entry in self._subscribers.values():
            for item in entry.get("targets") or []:
                uid = str(item.get("uid") or "")
                if not uid:
                    continue
                record = merged.setdefault(
                    uid,
                    {"uid": uid, "name": "", "groups": 0},
                )
                record["groups"] += 1
                if not record["name"] and item.get("name"):
                    record["name"] = str(item["name"])
        return [merged[uid] for uid in sorted(merged)]

    @staticmethod
    def _parse_up_uid(text: str) -> str:
        """从指令参数里解析出 UP 的 uid。

        Args:
            text: 形如 ``161775300`` / ``https://space.bilibili.com/161775300?spm=...``。

        Returns:
            uid 字符串；解析不出时返回空串。
        """
        cleaned = text.strip()
        if not cleaned:
            return ""
        head = cleaned.split()[0]
        if head.isdigit():
            return head
        matched = re.search(r"space\.bilibili\.com/(\d+)", cleaned)
        return matched.group(1) if matched else ""

    # ===== 动态分发（按群订阅筛选） =====

    def _log_dispatch(self, message: str) -> None:
        """记一条分发记录（同时写插件日志与控制台页面）。

        Args:
            message: 记录内容，例如「首次加载 12 条动态，按配置跳过分发」。
        """
        stamp = time.strftime("%H:%M:%S")
        self.dispatch_logs.append({"time": stamp, "message": message})
        logger.info(f"[bilibili] {message}")

    async def _on_dynamics(self, kind: str, items: list[dict]) -> None:
        """桥接进程捕获到动态时的处理入口（库不筛选，这里按群订阅筛）。

        新动态的判定不看内核给的 ``kind`` 标签（页面重载后的第一批也叫 ``INIT``），
        而是用**上一次已见动态里最新的发布时间**做水位线：

        * 本次运行的第一批：默认整批当历史动态跳过，只把最新的时间记为水位；
        * 之后的每一批：只有发布时间晚于水位的才当作新动态分发；
        * 水位的粒度是「秒」，所以同一秒发布的多条动态会一起被当成新一轮。

        Args:
            kind: 捕获类型，``INIT``（首次加载）或 ``UPDATE``（轮询更新）。
            items: 本次捕获到的动态列表（关注流全部 UP）。
        """
        newest = max((float(item.get("pubTs") or 0) for item in items), default=0.0)

        if self._dynamic_watermark is None:
            # 从未获取过（首次安装/删除过状态）：整批当历史动态，只把最新时间记为水位
            self._dynamic_watermark = newest
            self._save_watermark()
            if not bool(self.config.get("dispatch_initial", False)):
                self._log_dispatch(
                    f"首次加载 {len(items)} 条动态（没有历史水位），"
                    "按配置跳过分发（dispatch_initial=false）",
                )
                return
        else:
            fresh = [
                item
                for item in items
                if float(item.get("pubTs") or 0) > self._dynamic_watermark
            ]
            if len(fresh) != len(items):
                self._log_dispatch(
                    f"捕获 {len(items)} 条动态（{kind}），"
                    f"其中 {len(items) - len(fresh)} 条不晚于上次已见动态，按旧动态跳过",
                )
            if not fresh:
                return
            if newest > self._dynamic_watermark:
                self._dynamic_watermark = newest
                self._save_watermark()
            items = fresh

        # uid → [(群订阅记录, 该群里的订阅项)]，用于按作者精准分发
        index: dict[str, list[tuple[dict, dict]]] = {}
        for entry in self._subscribers.values():
            for target in entry.get("targets") or []:
                uid = str(target.get("uid") or "")
                if uid:
                    index.setdefault(uid, []).append((entry, target))

        if not index:
            self._log_dispatch(
                f"捕获 {len(items)} 条动态（{kind}），但没有任何群关注 UP"
                "（群管理员可用 /关注up <uid> 订阅）",
            )
            return

        limit = int(self.config.get("max_dispatch_per_round", 5) or 5)
        interval = float(self.config.get("dispatch_interval", 1.5) or 0)
        style = str(self.config.get("message_style") or STYLE_AUTO)
        selected = items[: max(limit, 1)]

        matched = 0
        sent = 0
        named = False
        for dynamic in selected:
            hits = index.get(str(dynamic.get("uid") or ""))
            if not hits:
                continue
            matched += 1
            for entry, target in hits:
                # 订阅时没取到名字的话，用动态里的作者名回填一次
                if not target.get("name") and dynamic.get("author"):
                    target["name"] = str(dynamic["author"])[:40]
                    named = True
                umo = str(entry.get("umo") or "")
                if not umo:
                    continue
                await self._send_dynamic(dynamic, entry, umo, style)
                sent += 1
                if interval > 0:
                    await asyncio.sleep(interval)

        if named:
            self._save_subscribers()
        if matched == 0:
            self._log_dispatch(
                f"捕获 {len(items)} 条动态（{kind}），没有一条命中已关注的 UP",
            )
        else:
            self._log_dispatch(
                f"捕获 {len(items)} 条动态（{kind}），命中 {matched} 条 → 共分发 {sent} 次",
            )

    async def _send_dynamic(
        self,
        dynamic: dict,
        target: dict,
        umo: str,
        style: str,
    ) -> None:
        """把一条动态发送到单个订阅群（发送失败时逐级降级）。

        Args:
            dynamic: 动态数据。
            target: 群订阅记录（含平台信息）。
            umo: 目标会话的 unified_msg_origin。
            style: 分发样式配置。
        """
        platform_name = str(target.get("platform_name") or "")
        group_id = target.get("group_id")
        dyn_id = str(dynamic.get("dynId") or "?")

        # auto：先自己画分享图（Pillow，本地渲染）；画不出来就退回 rich（文字 + 原图）
        used_style = style
        card_path = ""
        if style == STYLE_AUTO and dyn_id != "?":
            try:
                card_path = str(
                    await asyncio.to_thread(
                        render_share_card,
                        dynamic,
                        self.cards_dir / f"{dyn_id}.png",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 分享图渲染失败不阻断分发
                logger.warning(
                    f"[bilibili] 动态 {dyn_id} 分享图渲染失败: {exc}，改用图文消息",
                )
                used_style = STYLE_RICH

        try:
            await self.context.send_message(
                umo,
                build_dynamic_chain(dynamic, platform_name, used_style, card_path),
            )
            self._log_dispatch(f"已分发动态 {dyn_id} → 群 {group_id}（{used_style}）")
        except Exception as exc:  # noqa: BLE001 - 图片可能拉取失败，需降级重试
            logger.warning(
                f"[bilibili] 向 {group_id} 发送动态失败（{used_style}）: {exc}，降级为文本",
            )
            if used_style == STYLE_TEXT:
                self._log_dispatch(f"❗ 动态 {dyn_id} → 群 {group_id} 发送失败：{exc}")
                return
            try:
                await self.context.send_message(
                    umo,
                    build_dynamic_chain(dynamic, platform_name, STYLE_TEXT),
                )
                self._log_dispatch(f"已分发动态 {dyn_id} → 群 {group_id}（降级为文本）")
            except Exception as fallback_exc:  # noqa: BLE001 - 降级仍失败时记录原因
                self._log_dispatch(
                    f"❗ 动态 {dyn_id} → 群 {group_id} 文本降级仍失败：{fallback_exc}",
                )
