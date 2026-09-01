from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from queue import SimpleQueue
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


class LocalAgentError(RuntimeError):
    pass


class LocalAgentClient:
    """Small loopback client used by the native Windows control panel."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.health().get("ok") is True:
                    return True
            except Exception:
                time.sleep(0.25)
        return False

    def wait_until_stopped(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.health()
            except Exception:
                return True
            time.sleep(0.25)
        return False

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def status(self) -> dict:
        return self._request("GET", "/api/status")

    def tasks(self) -> list[dict]:
        return self._request("GET", "/api/tasks")

    def create_local_schedule(self, payload: dict) -> dict:
        return self._request("POST", "/api/local-schedules", json=payload, timeout=30.0)

    def update_local_schedule(self, task_id: str, payload: dict) -> dict:
        return self._request(
            "PUT",
            f"/api/local-schedules/{task_id}",
            json=payload,
            timeout=30.0,
        )

    def cancel_local_schedule(self, task_id: str) -> dict:
        return self._request("POST", f"/api/local-schedules/{task_id}/cancel")

    def logs(
        self,
        *,
        level: str = "ERROR",
        limit: int = 300,
        query: str = "",
    ) -> dict:
        return self._request(
            "GET",
            "/api/logs",
            params={"level": level, "limit": limit, "query": query},
        )

    def preflight(self) -> dict:
        return self._request("POST", "/api/preflight", timeout=25.0)

    def identify_wechat(self) -> dict:
        return self._request("POST", "/api/wechat/identify", timeout=50.0)

    def shutdown(self) -> dict:
        return self._request("POST", "/api/shutdown", timeout=5.0)

    def test_source(self, source_id: str) -> dict:
        return self._request("POST", f"/api/sources/{source_id}/test", timeout=20.0)

    def add_source(self, payload: dict) -> dict:
        return self._request("POST", "/api/sources", json=payload)

    def update_source(self, source_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/api/sources/{source_id}", json=payload)

    def delete_source(self, source_id: str) -> dict:
        return self._request("DELETE", f"/api/sources/{source_id}")

    def test_wechat_sync(self, profile_id: str) -> dict:
        return self._request(
            "POST",
            f"/api/connectors/wechatsync/{profile_id}/test",
            timeout=40.0,
        )

    def launch_wechat_sync(self, profile_id: str) -> dict:
        return self._request("POST", f"/api/connectors/wechatsync/{profile_id}/launch")

    def wechat_sync_token(self, profile_id: str) -> dict:
        return self._request("GET", f"/api/connectors/wechatsync/{profile_id}/token")

    def add_wechat_sync(self, payload: dict) -> dict:
        return self._request("POST", "/api/connectors/wechatsync", json=payload)

    def update_wechat_sync(self, profile_id: str, payload: dict) -> dict:
        return self._request(
            "PUT",
            f"/api/connectors/wechatsync/{profile_id}",
            json=payload,
        )

    def delete_wechat_sync(self, profile_id: str) -> dict:
        return self._request("DELETE", f"/api/connectors/wechatsync/{profile_id}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                params=params,
                headers={"X-Local-Agent-Action": "confirmed"},
                timeout=timeout or self.timeout,
            )
        except httpx.TimeoutException as error:
            raise LocalAgentError("本机 Agent 响应超时，请稍后重试") from error
        except httpx.HTTPError as error:
            raise LocalAgentError("无法连接本机 Agent，请从开始菜单重新启动") from error

        if response.is_success:
            return response.json() if response.content else None
        try:
            body = response.json()
        except ValueError:
            body = response.text
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message") or str(detail)
            detail = f"[{code}] {message}" if code else message
        raise LocalAgentError(str(detail or f"HTTP {response.status_code}"))


def stop_agent_backend(client: LocalAgentClient, timeout: float = 15.0) -> None:
    """Request a safe shutdown and wait until the backend is actually gone."""
    try:
        client.shutdown()
    except LocalAgentError:
        # The server can close its socket before returning the shutdown response.
        if client.wait_until_stopped(1.0):
            return
        raise
    if not client.wait_until_stopped(timeout):
        raise LocalAgentError(
            f"Agent 未能在 {timeout:g} 秒内退出，请确认没有正在执行的发布任务"
        )


@dataclass
class Palette:
    paper: str = "#F2EEE2"
    panel: str = "#FFFDF7"
    ink: str = "#18231F"
    muted: str = "#68736D"
    line: str = "#C9C1B2"
    signal: str = "#D84B2F"
    ok: str = "#1F7A55"
    soft: str = "#E8E1D2"


class NativeAdminWindow:
    """Native Tk control panel; no browser or embedded web view is used."""

    def __init__(self, client: LocalAgentClient) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.client = client
        self.colors = Palette()
        self.root = tk.Tk()
        self.root.title("微信小助手")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = max(920, min(1180, screen_width - 40))
        window_height = max(600, min(760, screen_height - 100))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(920, 560)
        self.root.configure(bg=self.colors.paper)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        try:
            import sys

            self.root.iconbitmap(default=sys.executable)
        except Exception:
            pass

        self._closing = False
        self._refreshing = False
        self._status: dict = {}
        self._sources: list[dict] = []
        self._profiles: list[dict] = []
        self._tasks: list[dict] = []
        self._log_dialog = None
        self._facts: dict[str, Any] = {}
        self._scroll_canvases: list[Any] = []
        self._callbacks: SimpleQueue[Callable] = SimpleQueue()
        self._configure_styles()
        self._build()
        self.root.bind_all("<MouseWheel>", self._scroll_active_panel, add="+")
        self.root.after_idle(self._reset_panel_scroll_positions)
        self.root.after(50, self._drain_callbacks)

    def run(self) -> int:
        self.refresh()
        self.root.mainloop()
        return 0

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure(
            "Agent.Treeview",
            background=self.colors.panel,
            fieldbackground=self.colors.panel,
            foreground=self.colors.ink,
            rowheight=26,
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Agent.Treeview.Heading",
            background=self.colors.soft,
            foreground=self.colors.ink,
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("Agent.Treeview", background=[("selected", "#D8E6DC")])
        style.configure(
            "Agent.TCombobox",
            fieldbackground=self.colors.panel,
            background=self.colors.panel,
        )

    def _build(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg=self.colors.paper, padx=16, pady=12)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.colors.paper)
        header.pack(fill="x", pady=(0, 8))
        title_group = tk.Frame(header, bg=self.colors.paper)
        title_group.pack(side="left")
        tk.Label(
            title_group,
            text="WINDOWS AGENT / NATIVE CONTROL",
            bg=self.colors.paper,
            fg=self.colors.signal,
            font=("Bahnschrift SemiBold", 9),
        ).pack(anchor="w")
        tk.Label(
            title_group,
            text="微信小助手",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 27, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="原生桌面控制台\n本地 API 仅供内部通信",
            justify="left",
            bg=self.colors.panel,
            fg=self.colors.ink,
            bd=1,
            relief="solid",
            padx=10,
            pady=5,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", anchor="n")

        rule = tk.Frame(outer, height=2, bg=self.colors.ink)
        rule.pack(fill="x", pady=(0, 8))

        body = tk.PanedWindow(
            outer,
            orient="horizontal",
            sashwidth=8,
            bg=self.colors.paper,
            bd=0,
        )
        body.pack(fill="both", expand=True)
        left_container, left = self._scrollable_panel(body, width=300)
        right_container, right = self._scrollable_panel(body)
        body.add(left_container, minsize=270, width=310)
        body.add(right_container, minsize=560)
        self._build_status(left)
        self._build_workspace(right)

    def _scrollable_panel(self, parent, *, width: int | None = None):
        container = self.tk.Frame(
            parent,
            bg=self.colors.panel,
            bd=1,
            relief="solid",
            width=width,
        )
        canvas = self.tk.Canvas(
            container,
            bg=self.colors.panel,
            bd=0,
            highlightthickness=0,
        )
        scrollbar = self.ttk.Scrollbar(
            container,
            orient="vertical",
            command=canvas.yview,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = self.tk.Frame(
            canvas,
            bg=self.colors.panel,
            padx=12,
            pady=10,
        )
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def resize_content(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def update_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", resize_content)
        content.bind("<Configure>", update_scroll_region)
        self._scroll_canvases.append(canvas)
        return container, content

    def _scroll_active_panel(self, event):
        pointer_x, pointer_y = self.root.winfo_pointerxy()
        for canvas in self._scroll_canvases:
            left = canvas.winfo_rootx()
            top = canvas.winfo_rooty()
            if (
                left <= pointer_x < left + canvas.winfo_width()
                and top <= pointer_y < top + canvas.winfo_height()
            ):
                delta = -1 if event.delta > 0 else 1
                canvas.yview_scroll(delta * 3, "units")
                return "break"
        return None

    def _reset_panel_scroll_positions(self) -> None:
        for canvas in self._scroll_canvases:
            canvas.yview_moveto(0.0)

    def _build_status(self, parent) -> None:
        tk = self.tk
        tk.Label(
            parent,
            text="本机状态",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 20, "bold"),
        ).pack(anchor="w", pady=(0, 6))
        facts = tk.Frame(parent, bg=self.colors.panel)
        facts.pack(fill="x")
        for key, label in (
            ("agent", "Agent"),
            ("account", "微信账号"),
            ("wechat_id", "微信号"),
            ("identity", "识别状态"),
            ("wechat", "微信"),
            ("login", "登录"),
            ("desktop", "桌面"),
            ("worker", "Worker"),
            ("outbox", "Outbox"),
            ("cache", "媒体缓存"),
            ("alerts", "告警"),
        ):
            row = tk.Frame(facts, bg=self.colors.panel)
            row.pack(fill="x")
            tk.Label(
                row,
                text=label,
                bg=self.colors.panel,
                fg=self.colors.ink,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", anchor="n", pady=3)
            value = tk.Label(
                row,
                text="-",
                bg=self.colors.panel,
                fg=self.colors.muted,
                justify="right",
                wraplength=190,
                font=("Microsoft YaHei UI", 9),
            )
            value.pack(side="right", anchor="n", pady=3)
            self._facts[key] = value
            tk.Frame(row, height=1, bg=self.colors.soft).pack(
                side="bottom", fill="x"
            )

        buttons = tk.Frame(parent, bg=self.colors.panel)
        buttons.pack(fill="x", pady=(8, 4))
        self._button(buttons, "环境预检", self.preflight).grid(row=0, column=0, padx=(0, 4), pady=2)
        self._button(buttons, "重新识别", self.identify, alt=True).grid(row=0, column=1, padx=4, pady=2)
        self._button(buttons, "刷新", self.refresh, alt=True).grid(row=1, column=0, padx=(0, 4), pady=2)
        self._button(buttons, "安全退出 Agent", self.shutdown, danger=True).grid(row=1, column=1, padx=4, pady=2)
        self._button(buttons, "查看错误日志", self.open_logs, alt=True).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, 6),
            pady=2,
        )

        self.notice_label = tk.Label(
            parent,
            text="正在连接本机 Agent...",
            bg=self.colors.panel,
            fg=self.colors.muted,
            justify="left",
            anchor="nw",
            wraplength=290,
            font=("Microsoft YaHei UI", 9),
        )
        self.notice_label.pack(fill="x", pady=(4, 0))

    def _build_workspace(self, parent) -> None:
        tk = self.tk
        source_head = tk.Frame(parent, bg=self.colors.panel)
        source_head.pack(fill="x")
        tk.Label(
            source_head,
            text="内容数据源",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 20, "bold"),
        ).pack(side="left")
        self._button(source_head, "+ 添加来源", self.open_source).pack(side="right")

        source_tools = tk.Frame(parent, bg=self.colors.panel)
        source_tools.pack(fill="x", pady=(5, 5))
        self._button(source_tools, "测试连接", self.test_source, alt=True).pack(side="left", padx=(0, 6))
        self._button(source_tools, "编辑 URL / 凭据", self.edit_source, alt=True).pack(side="left", padx=6)
        self._button(source_tools, "删除", self.delete_source, danger=True).pack(side="left", padx=6)

        source_columns = ("name", "state", "account", "url")
        self.source_tree = self.ttk.Treeview(
            parent,
            columns=source_columns,
            show="headings",
            height=3,
            style="Agent.Treeview",
        )
        for column, title, width in (
            ("name", "数据源", 170),
            ("state", "状态", 100),
            ("account", "账号", 110),
            ("url", "URL", 360),
        ):
            self.source_tree.heading(column, text=title)
            self.source_tree.column(column, width=width, minwidth=70)
        self.source_tree.pack(fill="x")

        connector_head = tk.Frame(parent, bg=self.colors.panel)
        connector_head.pack(fill="x", pady=(8, 0))
        tk.Label(
            connector_head,
            text="浏览器发布账号",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 18, "bold"),
        ).pack(side="left")
        self._button(connector_head, "+ Chrome Profile", self.open_wechat_sync).pack(side="right")

        connector_tools = tk.Frame(parent, bg=self.colors.panel)
        connector_tools.pack(fill="x", pady=(4, 4))
        self._button(connector_tools, "检测登录", self.test_wechat_sync, alt=True).pack(side="left", padx=(0, 5))
        self._button(connector_tools, "启动 Chrome", self.launch_wechat_sync, alt=True).pack(side="left", padx=5)
        self._button(connector_tools, "复制连接配置", self.copy_wechat_sync_setup, alt=True).pack(side="left", padx=5)
        self._button(connector_tools, "编辑", self.edit_wechat_sync, alt=True).pack(side="left", padx=5)
        self._button(connector_tools, "删除", self.delete_wechat_sync, danger=True).pack(side="left", padx=5)

        connector_columns = ("name", "state", "accounts", "bridge")
        self.connector_tree = self.ttk.Treeview(
            parent,
            columns=connector_columns,
            show="headings",
            height=3,
            style="Agent.Treeview",
        )
        for column, title, width in (
            ("name", "Chrome Profile", 170),
            ("state", "连接", 90),
            ("accounts", "已登录账号", 280),
            ("bridge", "本机 Bridge", 210),
        ):
            self.connector_tree.heading(column, text=title)
            self.connector_tree.column(column, width=width, minwidth=70)
        self.connector_tree.pack(fill="x")

        task_head = tk.Frame(parent, bg=self.colors.panel)
        task_head.pack(fill="x", pady=(8, 5))
        tk.Label(
            task_head,
            text="最近任务",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 18, "bold"),
        ).pack(side="left")
        self._button(task_head, "刷新任务", self.refresh, alt=True).pack(side="right")
        self._button(task_head, "取消定时", self.cancel_local_schedule, danger=True).pack(
            side="right", padx=5
        )
        self._button(task_head, "编辑定时", self.edit_local_schedule, alt=True).pack(
            side="right", padx=5
        )
        self._button(task_head, "+ 定时朋友圈", self.open_local_schedule).pack(
            side="right", padx=5
        )

        task_columns = ("source", "task", "account", "state", "attempt", "updated")
        self.task_tree = self.ttk.Treeview(
            parent,
            columns=task_columns,
            show="headings",
            height=4,
            style="Agent.Treeview",
        )
        for column, title, width in (
            ("source", "来源", 130),
            ("task", "任务 / 文案", 210),
            ("account", "微信账号", 120),
            ("state", "状态", 100),
            ("attempt", "尝试", 60),
            ("updated", "执行 / 更新时间", 165),
        ):
            self.task_tree.heading(column, text=title)
            self.task_tree.column(column, width=width, minwidth=50)
        self.task_tree.pack(fill="both", expand=True)

    def _button(self, parent, text: str, command: Callable, *, alt=False, danger=False):
        background = self.colors.panel if alt else self.colors.ink
        foreground = self.colors.ink if alt else "white"
        if danger:
            background = self.colors.signal
            foreground = "white"
        return self.tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=self.colors.soft if alt else "#33463E",
            activeforeground=self.colors.ink if alt else "white",
            relief="solid" if alt else "flat",
            bd=1 if alt else 0,
            padx=9,
            pady=5,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def refresh(self) -> None:
        if self._refreshing or self._closing:
            return
        self._refreshing = True

        def load():
            return self.client.status(), self.client.tasks()

        def loaded(result):
            self._refreshing = False
            self._status, tasks = result
            self._sources = self._status.get("sources", [])
            self._profiles = self._status.get("connectors", [])
            self._render_status()
            self._render_sources()
            self._render_connectors()
            self._render_tasks(tasks)
            self.notice("Agent 已连接，桌面控制台会自动刷新", "ok")
            if not self._closing:
                self.root.after(10000, self.refresh)

        def failed(error):
            self._refreshing = False
            self.notice(str(error), "error")
            if not self._closing:
                self.root.after(3000, self.refresh)

        self._async(load, loaded, failed)

    def _render_status(self) -> None:
        status = self._status
        wechat = status.get("wechat", {})
        identity = wechat.get("identityRecognition") or {}
        values = {
            "agent": status.get("agent", {}).get("displayName", "-"),
            "account": wechat.get("wechatNickname") or ("识别中..." if identity.get("state") == "detecting" else "待识别"),
            "wechat_id": wechat.get("wechatId") or "-",
            "identity": identity.get("message") or "-",
            "wechat": "运行中" if wechat.get("running") else "未运行",
            "login": "已登录" if wechat.get("loggedIn") else "待检查",
            "desktop": "可交互" if wechat.get("desktopUnlocked") else "已锁定",
            "worker": "执行中" if status.get("worker", {}).get("active") else "空闲",
            "outbox": f"{status.get('outbox', {}).get('backlog', 0)} 条",
            "cache": f"{round(status.get('mediaCache', {}).get('bytes', 0) / 1024 / 1024)} MiB",
            "alerts": f"{len(status.get('alerts', []))} 条",
        }
        for key, value in values.items():
            self._facts[key].configure(text=value)

    def _render_sources(self) -> None:
        selected = self._selected_source_id()
        self.source_tree.delete(*self.source_tree.get_children())
        for source in self._sources:
            state = source.get("healthState", "unknown")
            credential = "" if source.get("hasCredential") else " / 待配置凭据"
            self.source_tree.insert(
                "",
                "end",
                iid=source["id"],
                values=(
                    source.get("name", source["id"]),
                    state + credential,
                    source.get("accountKey", "-"),
                    source.get("baseUrl", "-"),
                ),
            )
        if selected and self.source_tree.exists(selected):
            self.source_tree.selection_set(selected)

    def _render_tasks(self, tasks: list[dict]) -> None:
        selected_task = self._selected_task()
        selected_id = selected_task.get("task_id") if selected_task else None
        self._tasks = tasks
        self.task_tree.delete(*self.task_tree.get_children())
        for index, task in enumerate(tasks):
            is_local = task.get("kind") == "local_schedule"
            label = task.get("text_preview") if is_local else task.get("task_id", "-")
            account = (
                task.get("target_nickname")
                or task.get("target_wechat_id")
                or task.get("target_account_key")
                or "-"
            )
            self.task_tree.insert(
                "",
                "end",
                iid=f"task-{index}",
                values=(
                    task.get("source_id", "-"),
                    label or task.get("task_id", "-"),
                    account,
                    self._task_state_label(task.get("state", "-")) if is_local else task.get("state", "-"),
                    task.get("attempt", "-"),
                    self._format_local_time(task.get("scheduled_at"))
                    if is_local
                    else task.get("updated_at", "-"),
                ),
            )
            if selected_id and task.get("task_id") == selected_id:
                self.task_tree.selection_set(f"task-{index}")

    @staticmethod
    def _format_local_time(value: str | None) -> str:
        if not value:
            return "-"
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return value

    @staticmethod
    def _task_state_label(value: str) -> str:
        return {
            "pending": "待执行",
            "executing": "执行中",
            "final_click_intent": "点击中",
            "confirming": "确认中",
            "succeeded": "已成功",
            "failed": "失败",
            "uncertain": "待人工确认",
            "cancelled": "已取消",
        }.get(value, value)

    def open_local_schedule(self) -> None:
        self._local_schedule_dialog(None)

    def edit_local_schedule(self) -> None:
        task = self._selected_task()
        if task is None or task.get("kind") != "local_schedule":
            self.notice("请先选择一个本机定时任务", "error")
            return
        if task.get("state") not in {"pending", "failed"}:
            self.notice("只有待执行或点击前失败的本机任务可以编辑", "error")
            return
        self._local_schedule_dialog(task)

    def cancel_local_schedule(self) -> None:
        from tkinter import messagebox

        task = self._selected_task()
        if task is None or task.get("kind") != "local_schedule":
            self.notice("请先选择一个本机定时任务", "error")
            return
        if not messagebox.askyesno(
            "取消定时任务",
            "确定取消这个本机朋友圈定时任务？\n已复制到 Agent 的图片会保留作为审计记录。",
            parent=self.root,
        ):
            return

        def done(_result):
            self.notice("本机定时任务已取消", "ok")
            self.refresh()

        self._async(lambda: self.client.cancel_local_schedule(task["task_id"]), done)

    def _local_schedule_dialog(self, task: dict | None) -> None:
        tk = self.tk
        from tkinter import filedialog, messagebox

        dialog = tk.Toplevel(self.root)
        dialog.title("编辑定时朋友圈" if task else "新建定时朋友圈")
        dialog.geometry("760x690")
        dialog.minsize(680, 620)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.colors.paper, padx=24, pady=20)

        tk.Label(
            dialog,
            text="本机定时朋友圈",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 24, "bold"),
        ).pack(anchor="w")
        target = task or {}
        account = (
            target.get("target_nickname")
            or target.get("target_wechat_id")
            or self._status.get("wechat", {}).get("wechatNickname")
            or self._status.get("wechat", {}).get("wechatId")
            or "尚未识别"
        )
        tk.Label(
            dialog,
            text=f"指定微信账号：{account}。执行时账号不一致将停止发布。",
            bg=self.colors.paper,
            fg=self.colors.muted,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(3, 16))

        tk.Label(
            dialog,
            text="朋友圈文案（最多 5000 字）",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")
        text_box = tk.Text(
            dialog,
            height=12,
            wrap="word",
            bg=self.colors.panel,
            fg=self.colors.ink,
            relief="solid",
            bd=1,
            padx=10,
            pady=8,
            font=("Microsoft YaHei UI", 10),
        )
        text_box.pack(fill="both", expand=True, pady=(6, 14))
        text_box.insert("1.0", target.get("text", ""))

        schedule_row = tk.Frame(dialog, bg=self.colors.paper)
        schedule_row.pack(fill="x", pady=(0, 14))
        tk.Label(
            schedule_row,
            text="执行时间",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")
        default_time = datetime.now().astimezone() + timedelta(minutes=10)
        if target.get("scheduled_at"):
            default_time = datetime.fromisoformat(
                target["scheduled_at"].replace("Z", "+00:00")
            ).astimezone()
        time_var = tk.StringVar(value=default_time.strftime("%Y-%m-%d %H:%M"))
        tk.Entry(
            schedule_row,
            textvariable=time_var,
            width=24,
            bg=self.colors.panel,
            fg=self.colors.ink,
            relief="solid",
            bd=1,
            font=("Consolas", 10),
        ).pack(side="left", padx=12, ipady=6)
        tk.Label(
            schedule_row,
            text="本机时区，格式 YYYY-MM-DD HH:MM",
            bg=self.colors.paper,
            fg=self.colors.muted,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

        image_paths: list[str] = []
        image_row = tk.Frame(dialog, bg=self.colors.paper)
        image_row.pack(fill="x")
        image_label = tk.Label(
            image_row,
            text="",
            bg=self.colors.paper,
            fg=self.colors.muted,
            justify="left",
            anchor="w",
            wraplength=520,
            font=("Microsoft YaHei UI", 9),
        )
        image_label.pack(side="left", fill="x", expand=True)

        def render_images() -> None:
            paths = image_paths or target.get("media_paths", [])
            prefix = "新选择" if image_paths else ("原任务" if task else "尚未选择")
            names = "、".join(os.path.basename(path) for path in paths)
            image_label.configure(text=f"{prefix}图片 {len(paths)} 张：{names}" if paths else prefix)

        def choose_images() -> None:
            chosen = filedialog.askopenfilenames(
                parent=dialog,
                title="选择 1 到 9 张朋友圈图片",
                filetypes=(
                    ("JPG / PNG 图片", "*.jpg *.jpeg *.png"),
                    ("所有文件", "*.*"),
                ),
            )
            if not chosen:
                return
            if len(chosen) > 9:
                messagebox.showerror("图片过多", "每条朋友圈最多选择 9 张图片。", parent=dialog)
                return
            image_paths[:] = list(chosen)
            render_images()

        self._button(image_row, "选择图片", choose_images, alt=True).pack(side="right", padx=(12, 0))
        render_images()

        actions = tk.Frame(dialog, bg=self.colors.paper)
        actions.pack(fill="x", pady=(18, 0))
        self._button(actions, "取消", dialog.destroy, alt=True).pack(side="right", padx=(6, 0))

        def save() -> None:
            try:
                local_time = datetime.strptime(time_var.get().strip(), "%Y-%m-%d %H:%M").astimezone()
                content = text_box.get("1.0", "end-1c")
                if len(content) > 5000:
                    raise ValueError("朋友圈文案不能超过 5000 字")
                if task is None and not image_paths:
                    raise ValueError("请选择 1 到 9 张图片")
                payload = {
                    "text": content,
                    "scheduledAt": local_time.isoformat(),
                }
                if task is None or image_paths:
                    payload["imagePaths"] = image_paths
            except ValueError as error:
                messagebox.showerror("内容不完整", str(error), parent=dialog)
                return

            save_button.configure(state="disabled")

            def saved(result):
                dialog.destroy()
                nickname = result.get("target_nickname") or result.get("target_wechat_id") or "当前微信"
                self.notice(f"已为 {nickname} 保存本机定时朋友圈", "ok")
                self.refresh()

            def failed(error):
                save_button.configure(state="normal")
                messagebox.showerror("保存失败", str(error), parent=dialog)

            operation = (
                (lambda: self.client.update_local_schedule(task["task_id"], payload))
                if task
                else (lambda: self.client.create_local_schedule(payload))
            )
            self._async(operation, saved, failed)

        save_button = self._button(actions, "保存定时任务", save)
        save_button.pack(side="right", padx=6)

    def _render_connectors(self) -> None:
        selected = self._selected_profile_id()
        self.connector_tree.delete(*self.connector_tree.get_children())
        for profile in self._profiles:
            account_text = " / ".join(
                f"{item.get('platform')}:{item.get('nickname')}"
                for item in profile.get("accounts", [])
            ) or "未识别到登录账号"
            self.connector_tree.insert(
                "",
                "end",
                iid=profile["id"],
                values=(
                    profile.get("name", profile["id"]),
                    "已连接" if profile.get("connected") else "等待扩展",
                    account_text,
                    profile.get("bridgeUrl", "-"),
                ),
            )
        if selected and self.connector_tree.exists(selected):
            self.connector_tree.selection_set(selected)

    def preflight(self) -> None:
        self.notice("正在执行环境预检，最多等待 25 秒...", "info")

        def done(result):
            ready = result.get("momentsWindowReady")
            self.notice(
                "预检完成：微信%s，桌面%s，朋友圈%s"
                % (
                    "运行中" if result.get("running") else "未运行",
                    "可交互" if result.get("desktopUnlocked") else "不可交互",
                    "就绪" if ready else "未就绪",
                ),
                "ok" if ready else "error",
            )
            self.refresh()

        self._async(self.client.preflight, done)

    def identify(self) -> None:
        self.notice("正在识别微信昵称，请暂时不要操作鼠标...", "info")

        def done(result):
            if result.get("recognized"):
                account = result.get("nickname") or "已识别"
                wechat_id = result.get("wechatId")
                self.notice(f"识别成功：{account}{' / ' + wechat_id if wechat_id else ''}", "ok")
            else:
                diagnostic = result.get("diagnostic") or {}
                self.notice(diagnostic.get("message", "未识别到微信账号"), "error")
            self.refresh()

        self._async(self.client.identify_wechat, done)

    def shutdown(self) -> None:
        self._request_shutdown()

    def _on_window_close(self) -> None:
        self._request_shutdown()

    def _request_shutdown(self) -> None:
        from tkinter import messagebox

        if self._closing:
            return
        if not messagebox.askyesno(
            "退出微信小助手",
            "关闭窗口将同时退出后台 Agent，确定继续？\n\n"
            "已领取的发布任务执行期间会拒绝退出；微信不会被关闭。",
            parent=self.root,
        ):
            return
        self._closing = True
        self.notice("正在安全停止后台 Agent，请稍候...", "info")

        def done(_result):
            self.notice("后台 Agent 已退出，正在关闭窗口", "ok")
            self.root.after(100, self.root.destroy)

        def failed(error):
            self._closing = False
            self.notice(str(error), "error")
            messagebox.showerror("无法退出微信小助手", str(error), parent=self.root)
            self.root.after(1000, self.refresh)

        self._async(lambda: stop_agent_backend(self.client), done, failed)

    def open_source(self) -> None:
        self._source_dialog(None)

    def open_logs(self) -> None:
        if self._log_dialog is not None and self._log_dialog.winfo_exists():
            self._log_dialog.deiconify()
            self._log_dialog.lift()
            self._log_dialog.focus_force()
            return

        tk = self.tk
        from tkinter import messagebox

        dialog = tk.Toplevel(self.root)
        self._log_dialog = dialog
        dialog.title("错误日志 - Windows Agent")
        dialog.geometry("980x680")
        dialog.minsize(760, 520)
        dialog.transient(self.root)
        dialog.configure(bg=self.colors.paper, padx=22, pady=18)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        header = tk.Frame(dialog, bg=self.colors.paper)
        header.pack(fill="x", pady=(0, 12))
        tk.Label(
            header,
            text="Agent 运行日志",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 24, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="默认只显示错误；日志按时间从旧到新排列",
            bg=self.colors.paper,
            fg=self.colors.muted,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=16, pady=(8, 0))

        tools = tk.Frame(dialog, bg=self.colors.paper)
        tools.pack(fill="x", pady=(0, 10))
        level_labels = {
            "错误及严重": "ERROR",
            "警告以上": "WARNING",
            "信息以上": "INFO",
            "全部": "ALL",
        }
        level_var = tk.StringVar(value="错误及严重")
        level_box = self.ttk.Combobox(
            tools,
            textvariable=level_var,
            values=tuple(level_labels),
            state="readonly",
            width=12,
            style="Agent.TCombobox",
        )
        level_box.pack(side="left", padx=(0, 8), ipady=3)
        query_var = tk.StringVar()
        query_entry = tk.Entry(
            tools,
            textvariable=query_var,
            bg=self.colors.panel,
            fg=self.colors.ink,
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
        )
        query_entry.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=6)
        query_entry.insert(0, "")

        viewer_frame = tk.Frame(dialog, bg=self.colors.panel, bd=1, relief="solid")
        viewer_frame.pack(fill="both", expand=True)
        viewer = tk.Text(
            viewer_frame,
            wrap="none",
            bg="#151B19",
            fg="#E8EEE9",
            insertbackground="white",
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 9),
            state="disabled",
        )
        y_scroll = self.ttk.Scrollbar(viewer_frame, orient="vertical", command=viewer.yview)
        x_scroll = self.ttk.Scrollbar(viewer_frame, orient="horizontal", command=viewer.xview)
        viewer.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")
        viewer.pack(side="left", fill="both", expand=True)
        viewer.tag_configure("ERROR", foreground="#FF8D79")
        viewer.tag_configure("CRITICAL", foreground="#FF5F55")
        viewer.tag_configure("WARNING", foreground="#F4C95D")
        viewer.tag_configure("INFO", foreground="#D8E5DC")
        viewer.tag_configure("DEBUG", foreground="#91A39A")

        footer = tk.Frame(dialog, bg=self.colors.paper)
        footer.pack(fill="x", pady=(10, 0))
        status_label = tk.Label(
            footer,
            text="准备读取日志...",
            bg=self.colors.paper,
            fg=self.colors.muted,
            anchor="w",
            font=("Microsoft YaHei UI", 9),
        )
        status_label.pack(side="left", fill="x", expand=True)
        state = {"directory": ""}

        def refresh_logs(*_args) -> None:
            status_label.configure(text="正在读取日志...", fg=self.colors.muted)

            def loaded(result):
                entries = result.get("entries", [])
                state["directory"] = result.get("logDirectory", "")
                viewer.configure(state="normal")
                viewer.delete("1.0", "end")
                if entries:
                    for entry in entries:
                        viewer.insert("end", entry.get("raw", "") + "\n\n", entry.get("level", "INFO"))
                    viewer.see("end")
                else:
                    viewer.insert("end", "没有符合条件的日志记录。\n", "INFO")
                viewer.configure(state="disabled")
                status_label.configure(
                    text=f"显示 {len(entries)} 条记录；最多返回 300 条",
                    fg=self.colors.ok,
                )

            def failed(error):
                status_label.configure(text=str(error), fg=self.colors.signal)

            self._async(
                lambda: self.client.logs(
                    level=level_labels[level_var.get()],
                    limit=300,
                    query=query_var.get().strip(),
                ),
                loaded,
                failed,
            )

        def open_directory() -> None:
            directory = state["directory"]
            if not directory:
                messagebox.showinfo("日志目录", "请先刷新日志以取得目录。", parent=dialog)
                return
            try:
                os.startfile(directory)
            except OSError as error:
                messagebox.showerror("无法打开日志目录", str(error), parent=dialog)

        def copy_logs() -> None:
            content = viewer.get("1.0", "end-1c")
            dialog.clipboard_clear()
            dialog.clipboard_append(content)
            status_label.configure(text="当前日志已复制到剪贴板", fg=self.colors.ok)

        self._button(tools, "查询", refresh_logs).pack(side="left", padx=(0, 6))
        self._button(tools, "打开日志目录", open_directory, alt=True).pack(side="left", padx=6)
        self._button(footer, "复制当前结果", copy_logs, alt=True).pack(side="right")
        query_entry.bind("<Return>", refresh_logs)
        level_box.bind("<<ComboboxSelected>>", refresh_logs)
        refresh_logs()

    def edit_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.notice("请先选择一个数据源", "error")
            return
        self._source_dialog(source)

    def test_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.notice("请先选择一个数据源", "error")
            return
        if not source.get("hasCredential"):
            self.notice("该数据源尚未配置 API Key", "error")
            return
        self.notice(f"正在测试 {source['name']}...", "info")

        def done(result):
            versions = ", ".join(result.get("versions", []))
            self.notice(f"连接成功：{result.get('sourceName')} / {versions}", "ok")
            self.refresh()

        self._async(lambda: self.client.test_source(source["id"]), done)

    def delete_source(self) -> None:
        from tkinter import messagebox

        source = self._selected_source()
        if source is None:
            self.notice("请先选择一个数据源", "error")
            return
        if not messagebox.askyesno(
            "删除数据源",
            f"删除 {source['name']}？\n本机保存的该来源凭据也会删除。",
            parent=self.root,
        ):
            return

        def done(_result):
            self.notice("数据源已删除", "ok")
            self.refresh()

        self._async(lambda: self.client.delete_source(source["id"]), done)

    def open_wechat_sync(self) -> None:
        self._wechat_sync_dialog(None)

    def edit_wechat_sync(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.notice("请先选择一个 Chrome Profile", "error")
            return
        self._wechat_sync_dialog(profile)

    def test_wechat_sync(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.notice("请先选择一个 Chrome Profile", "error")
            return
        self.notice(f"正在检测 {profile['name']} 的扩展与登录账号...", "info")

        def done(result):
            accounts = result.get("accounts", [])
            if result.get("connected"):
                names = "、".join(item.get("nickname", "-") for item in accounts) or "没有已登录账号"
                self.notice(f"扩展已连接：{names}", "ok" if accounts else "error")
            else:
                self.notice("扩展未连接，请在该 Chrome Profile 中开启 MCP 连接", "error")
            self.refresh()

        self._async(lambda: self.client.test_wechat_sync(profile["id"]), done)

    def launch_wechat_sync(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.notice("请先选择一个 Chrome Profile", "error")
            return
        self._async(
            lambda: self.client.launch_wechat_sync(profile["id"]),
            lambda _result: self.notice("Chrome 已启动，请登录平台并开启扩展 MCP 连接", "ok"),
        )

    def copy_wechat_sync_setup(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.notice("请先选择一个 Chrome Profile", "error")
            return

        def done(result):
            text = f"服务器地址: {profile['bridgeUrl']}\nToken: {result['token']}"
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.notice("扩展服务器地址与 Token 已复制，请粘贴到 WechatSync MCP 设置", "ok")

        self._async(lambda: self.client.wechat_sync_token(profile["id"]), done)

    def delete_wechat_sync(self) -> None:
        from tkinter import messagebox

        profile = self._selected_profile()
        if profile is None:
            self.notice("请先选择一个 Chrome Profile", "error")
            return
        if not messagebox.askyesno(
            "删除 Chrome Profile 配置",
            f"删除 {profile['name']}？\n不会删除 Chrome 用户数据。",
            parent=self.root,
        ):
            return

        def done(_result):
            self.notice("Chrome Profile 配置已删除", "ok")
            self.refresh()

        self._async(lambda: self.client.delete_wechat_sync(profile["id"]), done)

    def _wechat_sync_dialog(self, profile: dict | None) -> None:
        tk = self.tk
        from tkinter import messagebox

        dialog = tk.Toplevel(self.root)
        dialog.title("编辑 Chrome Profile" if profile else "添加 Chrome Profile")
        dialog.geometry("720x650")
        dialog.minsize(650, 600)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.colors.paper, padx=24, pady=20)

        values = {
            "id": tk.StringVar(value=(profile or {}).get("id", "")),
            "name": tk.StringVar(value=(profile or {}).get("name", "")),
            "port": tk.StringVar(value=str((profile or {}).get("bridgeUrl", "ws://127.0.0.1:9527").rsplit(":", 1)[-1])),
            "chrome": tk.StringVar(value=(profile or {}).get("chromeExecutable") or ""),
            "data": tk.StringVar(value=(profile or {}).get("userDataDir") or ""),
            "directory": tk.StringVar(value=(profile or {}).get("profileDirectory") or ""),
            "extension": tk.StringVar(value=(profile or {}).get("extensionPath") or ""),
            "enabled": tk.BooleanVar(value=(profile or {}).get("enabled", True)),
            "auto": tk.BooleanVar(value=(profile or {}).get("autoLaunch", False)),
            "zhihu": tk.BooleanVar(value="zhihu" in (profile or {}).get("platforms", ["zhihu", "juejin"])),
            "juejin": tk.BooleanVar(value="juejin" in (profile or {}).get("platforms", ["zhihu", "juejin"])),
        }

        tk.Label(
            dialog,
            text="一个 Profile 对应一套独立浏览器登录态",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 22, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        def field(row, title, variable, *, readonly=False):
            tk.Label(
                dialog,
                text=title,
                bg=self.colors.paper,
                fg=self.colors.ink,
                font=("Microsoft YaHei UI", 9, "bold"),
            ).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=7)
            entry = tk.Entry(
                dialog,
                textvariable=variable,
                state="readonly" if readonly else "normal",
                bg=self.colors.panel,
                fg=self.colors.ink,
                relief="solid",
                bd=1,
                font=("Microsoft YaHei UI", 10),
            )
            entry.grid(row=row, column=1, sticky="ew", pady=7, ipady=6)

        field(1, "Profile ID", values["id"], readonly=profile is not None)
        field(2, "显示名称", values["name"])
        field(3, "Bridge 端口", values["port"])
        field(4, "Chrome.exe（可留空）", values["chrome"])
        field(5, "独立 user-data-dir", values["data"])
        field(6, "profile-directory", values["directory"])
        field(7, "WechatSync 扩展目录", values["extension"])

        checks = tk.Frame(dialog, bg=self.colors.paper)
        checks.grid(row=8, column=1, sticky="w", pady=9)
        for title, variable in (
            ("启用", values["enabled"]),
            ("Agent 启动时打开 Chrome", values["auto"]),
            ("知乎", values["zhihu"]),
            ("掘金", values["juejin"]),
        ):
            tk.Checkbutton(
                checks,
                text=title,
                variable=variable,
                bg=self.colors.paper,
                activebackground=self.colors.paper,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", padx=(0, 14))

        tk.Label(
            dialog,
            text="安全约束：Bridge 固定监听 127.0.0.1；每个 Profile 必须使用不同端口。保存后用“复制连接配置”取得扩展 Token。",
            bg=self.colors.soft,
            fg=self.colors.ink,
            justify="left",
            wraplength=620,
            padx=12,
            pady=10,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 14))

        actions = tk.Frame(dialog, bg=self.colors.paper)
        actions.grid(row=10, column=0, columnspan=2, sticky="e")
        self._button(actions, "取消", dialog.destroy, alt=True).pack(side="left", padx=6)

        def save():
            platforms = [
                platform
                for platform, selected in (("zhihu", values["zhihu"]), ("juejin", values["juejin"]))
                if selected.get()
            ]
            payload = {
                "id": values["id"].get().strip(),
                "name": values["name"].get().strip(),
                "enabled": values["enabled"].get(),
                "bridgePort": values["port"].get().strip(),
                "platforms": platforms,
                "chromeExecutable": values["chrome"].get().strip() or None,
                "userDataDir": values["data"].get().strip() or None,
                "profileDirectory": values["directory"].get().strip() or None,
                "extensionPath": values["extension"].get().strip() or None,
                "autoLaunch": values["auto"].get(),
            }
            try:
                payload["bridgePort"] = int(payload["bridgePort"])
                if not payload["id"] or not payload["name"] or not platforms:
                    raise ValueError("Profile ID、名称和至少一个平台不能为空")
            except ValueError as error:
                messagebox.showerror("配置不完整", str(error), parent=dialog)
                return

            save_button.configure(state="disabled")

            def saved(_result):
                dialog.destroy()
                self.notice("Chrome Profile 配置已保存", "ok")
                self.refresh()

            def failed(error):
                save_button.configure(state="normal")
                messagebox.showerror("保存失败", str(error), parent=dialog)

            operation = (
                (lambda: self.client.update_wechat_sync(profile["id"], payload))
                if profile
                else (lambda: self.client.add_wechat_sync(payload))
            )
            self._async(operation, saved, failed)

        save_button = self._button(actions, "保存并应用", save)
        save_button.pack(side="left", padx=6)
        dialog.columnconfigure(1, weight=1)

    def _source_dialog(self, source: dict | None) -> None:
        tk = self.tk
        from tkinter import messagebox, ttk

        dialog = tk.Toplevel(self.root)
        dialog.title("编辑数据源" if source else "添加数据源")
        dialog.geometry("690x660")
        dialog.minsize(620, 610)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.colors.paper, padx=24, pady=20)

        values = {
            "id": tk.StringVar(value=(source or {}).get("id", "")),
            "name": tk.StringVar(value=(source or {}).get("name", "")),
            "url": tk.StringVar(value=(source or {}).get("baseUrl", "")),
            "account": tk.StringVar(value=(source or {}).get("accountKey", "wechat-main")),
            "weight": tk.StringVar(value=str((source or {}).get("weight", 1))),
            "type": tk.StringVar(value=(source or {}).get("type", "standard-http-v2")),
            "auth": tk.StringVar(value=(source or {}).get("authType", "api_key_header")),
            "header": tk.StringVar(value=(source or {}).get("headerName") or "x-api-key"),
            "credential": tk.StringVar(),
            "enabled": tk.BooleanVar(value=(source or {}).get("enabled", True)),
            "private": tk.BooleanVar(value=(source or {}).get("allowPrivateNetwork", False)),
        }

        tk.Label(
            dialog,
            text="编辑数据源 URL 与连接参数" if source else "注册新的内容数据源",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 23, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        def field(row, title, variable, *, secret=False, readonly=False):
            tk.Label(
                dialog,
                text=title,
                bg=self.colors.paper,
                fg=self.colors.ink,
                font=("Microsoft YaHei UI", 9, "bold"),
            ).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=7)
            entry = tk.Entry(
                dialog,
                textvariable=variable,
                show="*" if secret else "",
                state="readonly" if readonly else "normal",
                bg=self.colors.panel,
                fg=self.colors.ink,
                relief="solid",
                bd=1,
                font=("Microsoft YaHei UI", 10),
            )
            entry.grid(row=row, column=1, sticky="ew", pady=7, ipady=6)
            return entry

        field(1, "来源 ID", values["id"], readonly=source is not None)
        field(2, "显示名称", values["name"])
        field(3, "协议 Base URL", values["url"])
        field(4, "账号别名", values["account"])
        field(5, "权重 1-10", values["weight"])

        tk.Label(dialog, text="数据协议", bg=self.colors.paper, fg=self.colors.ink, font=("Microsoft YaHei UI", 9, "bold")).grid(row=6, column=0, sticky="w", padx=(0, 14), pady=7)
        type_box = ttk.Combobox(dialog, textvariable=values["type"], values=("standard-http-v2", "standard-http-v1"), state="readonly", style="Agent.TCombobox")
        type_box.grid(row=6, column=1, sticky="ew", pady=7, ipady=4)

        tk.Label(dialog, text="认证类型", bg=self.colors.paper, fg=self.colors.ink, font=("Microsoft YaHei UI", 9, "bold")).grid(row=7, column=0, sticky="w", padx=(0, 14), pady=7)
        auth_box = ttk.Combobox(dialog, textvariable=values["auth"], values=("api_key_header", "bearer"), state="readonly", style="Agent.TCombobox")
        auth_box.grid(row=7, column=1, sticky="ew", pady=7, ipady=4)
        field(8, "Header 名", values["header"])
        field(9, "凭据（留空保持原值）", values["credential"], secret=True)

        tk.Label(dialog, text="媒体域名白名单", bg=self.colors.paper, fg=self.colors.ink, font=("Microsoft YaHei UI", 9, "bold")).grid(row=10, column=0, sticky="nw", padx=(0, 14), pady=7)
        hosts = tk.Text(dialog, height=4, bg=self.colors.panel, fg=self.colors.ink, relief="solid", bd=1, font=("Consolas", 9))
        hosts.grid(row=10, column=1, sticky="nsew", pady=7)
        hosts.insert("1.0", "\n".join((source or {}).get("allowedHosts", [])))

        checks = tk.Frame(dialog, bg=self.colors.paper)
        checks.grid(row=11, column=1, sticky="w", pady=7)
        tk.Checkbutton(checks, text="启用来源", variable=values["enabled"], bg=self.colors.paper, activebackground=self.colors.paper, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 18))
        tk.Checkbutton(checks, text="允许私网媒体", variable=values["private"], bg=self.colors.paper, activebackground=self.colors.paper, font=("Microsoft YaHei UI", 9)).pack(side="left")

        actions = tk.Frame(dialog, bg=self.colors.paper)
        actions.grid(row=12, column=0, columnspan=2, sticky="e", pady=(16, 0))
        self._button(actions, "取消", dialog.destroy, alt=True).pack(side="left", padx=6)

        def save():
            source_id = values["id"].get().strip()
            base_url = values["url"].get().strip()
            allowed_hosts = [line.strip() for line in hosts.get("1.0", "end").splitlines() if line.strip()]
            if not allowed_hosts:
                hostname = urlparse(base_url).hostname
                allowed_hosts = [hostname] if hostname else []
            auth_type = values["auth"].get()
            payload = {
                "id": source_id,
                "name": values["name"].get().strip(),
                "type": values["type"].get(),
                "baseUrl": base_url,
                "enabled": values["enabled"].get(),
                "weight": values["weight"].get().strip(),
                "accountKey": values["account"].get().strip(),
                "auth": {
                    "type": auth_type,
                    "credentialRef": f"dpapi://{source_id}",
                    **({"headerName": values["header"].get().strip()} if auth_type == "api_key_header" else {}),
                },
                "mediaSecurity": {
                    "allowedHosts": allowed_hosts,
                    "allowPrivateNetwork": values["private"].get(),
                },
            }
            credential = values["credential"].get()
            if credential:
                payload["credential"] = credential
            try:
                payload["weight"] = int(payload["weight"])
                if not source_id or not payload["name"] or not base_url:
                    raise ValueError("来源 ID、名称和 URL 不能为空")
                if source is None and not credential:
                    raise ValueError("新增数据源必须填写凭据")
            except ValueError as error:
                messagebox.showerror("配置不完整", str(error), parent=dialog)
                return

            save_button.configure(state="disabled")

            def saved(_result):
                dialog.destroy()
                self.notice("数据源配置已保存并立即应用", "ok")
                self.refresh()

            def failed(error):
                save_button.configure(state="normal")
                messagebox.showerror("保存失败", str(error), parent=dialog)

            operation = (
                (lambda: self.client.update_source(source_id, payload))
                if source is not None
                else (lambda: self.client.add_source(payload))
            )
            self._async(operation, saved, failed)

        save_button = self._button(actions, "保存并应用", save)
        save_button.pack(side="left", padx=6)
        dialog.columnconfigure(1, weight=1)
        dialog.rowconfigure(10, weight=1)

    def _selected_source_id(self) -> str | None:
        selected = self.source_tree.selection() if hasattr(self, "source_tree") else ()
        return selected[0] if selected else None

    def _selected_source(self) -> dict | None:
        source_id = self._selected_source_id()
        return next((item for item in self._sources if item.get("id") == source_id), None)

    def _selected_profile_id(self) -> str | None:
        selected = self.connector_tree.selection() if hasattr(self, "connector_tree") else ()
        return selected[0] if selected else None

    def _selected_profile(self) -> dict | None:
        profile_id = self._selected_profile_id()
        return next((item for item in self._profiles if item.get("id") == profile_id), None)

    def _selected_task(self) -> dict | None:
        selected = self.task_tree.selection() if hasattr(self, "task_tree") else ()
        if not selected:
            return None
        try:
            index = int(selected[0].split("-", 1)[1])
        except (ValueError, IndexError):
            return None
        return self._tasks[index] if 0 <= index < len(self._tasks) else None

    def notice(self, message: str, kind: str = "info") -> None:
        color = {
            "ok": self.colors.ok,
            "error": self.colors.signal,
            "info": self.colors.muted,
        }.get(kind, self.colors.muted)
        self.notice_label.configure(text=message, fg=color)

    def _async(
        self,
        action: Callable,
        success: Callable | None = None,
        failure: Callable | None = None,
    ) -> None:
        def work():
            try:
                result = action()
            except Exception as error:
                callback = failure or (lambda exc: self.notice(str(exc), "error"))
                self._after(
                    lambda error=error, callback=callback: callback(error)
                )
                return
            if success is not None:
                self._after(lambda: success(result))

        threading.Thread(target=work, name="native-admin-action", daemon=True).start()

    def _after(self, callback: Callable) -> None:
        self._callbacks.put(callback)

    def _drain_callbacks(self) -> None:
        try:
            while not self._callbacks.empty():
                self._callbacks.get_nowait()()
            self.root.after(50, self._drain_callbacks)
        except self.tk.TclError:
            return


def run_native_admin(base_url: str) -> int:
    client = LocalAgentClient(base_url)
    if not client.wait_until_ready(15.0):
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "无法启动微信小助手",
            "本机 Agent 未能在 15 秒内启动。请检查 agent.log 或重新安装。",
            parent=root,
        )
        root.destroy()
        return 1
    return NativeAdminWindow(client).run()
