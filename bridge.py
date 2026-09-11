"""Node 桥接子进程管理。

插件本体（Python）不直接依赖 Node，而是把 ``bilibili-user-simulation`` 内核
交给 ``node/bridge.ts`` 子进程运行。本模块负责：

1. 以子进程方式拉起桥接脚本，并注入启动配置（人格、无头模式、用户数据目录等）；
2. 解析 stdout 上的协议行（前缀 ``@@BDYN@@``），把事件缓存成插件可查询的状态；
3. 把控制台下发的指令写进子进程 stdin，并等待对应的执行结果；
4. 插件卸载 / 手动关闭时优雅结束子进程。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

PROTOCOL_MARKER = "@@BDYN@@"
"""协议行前缀：只有带该前缀的 stdout 行会被当作事件解析。"""

READY_TIMEOUT_SECONDS = 180
"""等待内核就绪（浏览器拉起 + 登录态检测）的超时时间。"""

COMMAND_TIMEOUT_SECONDS = 300
"""单条控制台指令（含登录扫码等待）的超时时间。"""


class BridgeProcess:
    """``node/bridge.ts`` 子进程的生命周期与协议封装。

    Attributes:
        qrcode_png: 最近一次登录二维码（base64 PNG；无则空串）。
        status: 最近一次内核状态快照。
        dynamics: 最近捕获的动态（新的在前）。
        logs: 桥接进程日志（新的在后）。
        last_error: 最近一次失败原因（无则空串）。
    """

    def __init__(
        self,
        node_dir: Path,
        config: dict,
        on_dynamics: Callable[[str, list[dict]], Awaitable[None]] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        """初始化桥接管理器（不会启动进程，需调用 :meth:`start`）。

        Args:
            node_dir: 存放 ``bridge.ts`` 与 ``node_modules`` 的目录。
            config: 插件配置（读取 ``node_bin`` / ``headless`` / ``persona_*`` 等）。
            on_dynamics: 捕获到动态时的回调，参数为 ``(kind, items)``。
            data_dir: 插件数据目录（浏览器配置默认放在它的 ``browser_profile/`` 下）。
        """
        self.node_dir = node_dir
        self.config = config
        self.on_dynamics = on_dynamics
        self.data_dir = data_dir
        self.qrcode_png = ""
        self.status: dict[str, Any] = {}
        self.dynamics: deque[dict] = deque(maxlen=50)
        self.logs: deque[dict] = deque(maxlen=200)
        self.last_error = ""

        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._fatal = ""
        self._seq = 0

    @property
    def running(self) -> bool:
        """子进程是否仍在运行。"""
        return self._proc is not None and self._proc.returncode is None

    @property
    def node_bin(self) -> str:
        """Node 可执行文件路径（来自配置 ``node_bin``）。"""
        return str(self.config.get("node_bin") or "node").strip() or "node"

    def snapshot(self) -> dict:
        """返回供控制台页面展示的状态快照。

        Returns:
            包含进程状态、内核状态、二维码、最近动态与日志的字典。
        """
        return {
            "running": self.running,
            "qrcode": self.qrcode_png,
            "status": self.status,
            "last_error": self.last_error,
            "dynamics": list(self.dynamics)[:10],
            "logs": list(self.logs)[-60:],
        }

    async def start(self) -> None:
        """拉起桥接子进程并等待内核就绪。

        Raises:
            RuntimeError: Node 不可用、依赖未安装、内核初始化失败或超时。
        """
        if self.running:
            return

        node_bin = self.node_bin
        if shutil.which(node_bin) is None and not Path(node_bin).exists():
            raise RuntimeError(
                f"未找到 Node 可执行文件：{node_bin}（可在插件配置中修改 node_bin）"
            )

        script = self.node_dir / "bridge.ts"
        if not script.exists():
            raise RuntimeError(f"缺少桥接脚本：{script}")
        if not (self.node_dir / "node_modules").exists():
            raise RuntimeError(
                "Node 依赖未安装：请在插件控制台点「安装 Node 依赖」按钮"
                "（等价于在 node/ 目录执行 npm install，首次会下载 Chromium，耗时较长）",
            )

        self.qrcode_png = ""
        self.last_error = ""
        self._fatal = ""
        self._ready.clear()

        payload = {
            "headless": bool(self.config.get("headless", True)),
            "personaId": str(self.config.get("persona_id") or "ak-night-worker"),
            "personaFile": str(self.config.get("persona_file") or "").strip(),
            "personaDir": str(self.config.get("persona_dir") or "").strip(),
            "userDataDir": self._resolve_user_data_dir(),
            "chromePath": str(self.config.get("chrome_path") or "").strip(),
        }

        logger.info(f"[bilibili] 启动 Node 桥接进程：{node_bin} bridge.ts")
        self._proc = await asyncio.create_subprocess_exec(
            node_bin,
            "--import",
            "tsx",
            "bridge.ts",
            json.dumps(payload, ensure_ascii=False),
            cwd=str(self.node_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=READY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise RuntimeError(
                f"等待内核就绪超时（{READY_TIMEOUT_SECONDS}s），请查看插件日志排查浏览器启动问题",
            ) from exc

        if self._fatal:
            message = self._fatal
            await self.stop()
            raise RuntimeError(message)

    def _resolve_user_data_dir(self) -> str:
        """解析浏览器用户数据目录（持久化登录态）。

        配置了 ``user_data_dir`` 就用配置值；否则用插件数据目录下的
        ``browser_profile/``——不要落在依赖包目录里，否则 ``npm install``
        更新依赖时会把登录态一并冲掉。

        Returns:
            用户数据目录的绝对路径（未配置且无数据目录时返回空串，由库使用默认目录）。
        """
        configured = str(self.config.get("user_data_dir") or "").strip()
        if configured:
            return str(Path(configured).expanduser())
        if self.data_dir is not None:
            return str(self.data_dir / "browser_profile")
        return ""

    async def stop(self) -> None:
        """优雅关闭子进程（必要时强制结束），可重复调用。"""
        proc = self._proc
        if proc is None:
            return

        if proc.returncode is None and proc.stdin is not None:
            try:
                proc.stdin.write(b'{"id":"__shutdown__","line":"__shutdown__"}\n')
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("[bilibili] 桥接进程未在 20s 内退出，强制结束")
                proc.kill()
                await proc.wait()

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for task in self._dispatch_tasks:
            task.cancel()
        self._dispatch_tasks.clear()
        self._proc = None
        self._ready.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("桥接进程已关闭"))
        self._pending.clear()

    async def execute(
        self,
        line: str,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        wait: bool = True,
    ) -> dict:
        """向内核下发一条控制台指令。

        Args:
            line: 指令文本，例如 ``fetch on`` / ``sim on`` / ``status``。
            timeout: 等待结果的秒数。
            wait: 是否等待结果（``login`` 要等到扫码完成才返回，应传 False）。

        Returns:
            形如 ``{"ok": bool, "output": str}`` 的结果字典；``wait=False`` 时返回下发提示。

        Raises:
            RuntimeError: 进程未运行、写入失败或等待结果超时。
        """
        return await self._request({"line": line}, timeout=timeout, wait=wait)

    async def follow_up(
        self,
        uid: str,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ) -> dict:
        """在运行态主动关注一个 UP（内核 followUp，独立于模拟任务流）。

        Args:
            uid: UP 的 uid（纯数字）。
            timeout: 等待结果的秒数。

        Returns:
            ``{"ok": bool, "output": str, "data": {...}}``，``data`` 为内核返回的关注结果
            （``uid`` / ``name`` / ``status`` / ``detail``，名称由库在关注时从主页读取）。

        Raises:
            RuntimeError: 桥接进程未运行或等待结果超时。
        """
        return await self._request({"op": "follow", "uid": uid}, timeout=timeout)

    async def _request(
        self,
        payload: dict,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        wait: bool = True,
    ) -> dict:
        """向桥接进程下发一条请求并等待响应（所有通道共用）。

        Args:
            payload: 请求内容（``line`` 或 ``op``），会自动补上请求 id。
            timeout: 等待结果的秒数。
            wait: 是否等待结果。

        Returns:
            形如 ``{"ok": bool, "output": str, "data": dict}`` 的结果字典。

        Raises:
            RuntimeError: 进程未运行、写入失败或等待结果超时。
        """
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise RuntimeError("桥接进程未运行，请先在控制台启动引擎")

        self._seq += 1
        command_id = str(self._seq)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if wait:
            self._pending[command_id] = future
        try:
            data = json.dumps(
                {"id": command_id, **payload},
                ensure_ascii=False,
            )
            proc.stdin.write(data.encode("utf-8") + b"\n")
            await proc.stdin.drain()
            if not wait:
                return {"ok": True, "output": "指令已下发，结果会在状态中体现"}
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"操作 `{payload}` 执行超时（{timeout:.0f}s）") from exc
        finally:
            self._pending.pop(command_id, None)

    async def _read_stdout(self) -> None:
        """读取 stdout，把协议行分发成事件，其余输出记为普通日志。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                continue
            if not text.startswith(PROTOCOL_MARKER):
                self._append_log("info", text)
                continue
            try:
                event = json.loads(text[len(PROTOCOL_MARKER) :])
            except json.JSONDecodeError:
                self._append_log("warn", f"无法解析的协议行: {text[:200]}")
                continue
            await self._handle_event(event)
        self._mark_dead()

    async def _read_stderr(self) -> None:
        """读取 stderr（tsx / Node 的告警与崩溃信息）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text:
                self._append_log("warn", text)

    async def _handle_event(self, event: dict) -> None:
        """处理单条桥接事件。

        Args:
            event: 已解析的事件字典，``type`` 字段决定处理方式。
        """
        event_type = event.get("type")
        if event_type == "ready":
            self.status = event.get("status") or {}
            self._ready.set()
            logger.info(
                "[bilibili] 内核已就绪（蹲饼将返回关注流全部动态，筛选由插件负责）"
            )
        elif event_type == "status":
            self.status = event.get("status") or {}
        elif event_type == "qrcode":
            self.qrcode_png = str(event.get("png") or "")
            logger.info("[bilibili] 已获取登录二维码，请在控制台扫码")
        elif event_type == "log":
            level = str(event.get("level") or "info")
            self._append_log(level, str(event.get("message") or ""))
        elif event_type == "result":
            result_id = str(event.get("id") or "")
            future = self._pending.get(result_id)
            if future is not None and not future.done():
                data = event.get("data")
                future.set_result(
                    {
                        "ok": bool(event.get("ok")),
                        "output": str(event.get("output") or ""),
                        "data": data if isinstance(data, dict) else {},
                    },
                )
        elif event_type == "dynamics":
            items = event.get("items") or []
            if not isinstance(items, list):
                items = []
            # 内核回传的批次是「最新在前」，appendleft 逐个插入会把顺序倒过来（最旧的跑到最前），
            # 因此按倒序 extendleft，保证 self.dynamics[0] 始终是最新捕获的那条。
            batch = [item for item in items if isinstance(item, dict)]
            if batch:
                self.dynamics.extendleft(reversed(batch))
            if items and self.on_dynamics is not None:
                # 分发（含限速）在后台任务里进行，不能阻塞 stdout 读取和指令回传
                task = asyncio.create_task(
                    self._run_dynamics_callback(str(event.get("kind") or ""), items),
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
        elif event_type == "fatal":
            self._fatal = str(event.get("message") or "桥接进程初始化失败")
            self.last_error = self._fatal
            self._ready.set()
        elif event_type == "bye":
            logger.info("[bilibili] 内核已关闭")

    async def _run_dynamics_callback(self, kind: str, items: list[dict]) -> None:
        """在后台执行动态分发回调，并吞掉异常保证读取循环不中断。

        Args:
            kind: 捕获类型（INIT / UPDATE）。
            items: 本次捕获到的动态列表。
        """
        if self.on_dynamics is None:
            return
        try:
            await self.on_dynamics(kind, items)
        except Exception as exc:  # noqa: BLE001 - 分发失败不能影响桥接读取
            logger.error(f"[bilibili] 动态分发失败: {exc}")

    def _mark_dead(self) -> None:
        """子进程退出时的收尾：唤醒等待者并给出失败原因。"""
        proc = self._proc
        if proc is not None and proc.returncode not in (None, 0):
            self.last_error = f"桥接进程异常退出（code={proc.returncode}）"
            logger.error(f"[bilibili] {self.last_error}")
        if not self._ready.is_set():
            self._fatal = self.last_error or "桥接进程已退出"
            self._ready.set()
        for future in self._pending.values():
            if not future.done():
                future.set_result({"ok": False, "output": "桥接进程已退出"})
        self._pending.clear()

    def _append_log(self, level: str, message: str) -> None:
        """记录一条桥接日志。

        Args:
            level: 日志级别（info / warn / error）。
            message: 日志正文。
        """
        if not message:
            return
        self.logs.append({"level": level, "message": message})
        if level == "error":
            logger.error(f"[bilibili] {message}")
        elif level == "warn":
            logger.warning(f"[bilibili] {message}")
        else:
            logger.info(f"[bilibili] {message}")
