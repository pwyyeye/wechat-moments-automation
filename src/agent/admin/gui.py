from __future__ import annotations

import os
import threading
import time
from queue import SimpleQueue
from dataclasses import dataclass
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

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def status(self) -> dict:
        return self._request("GET", "/api/status")

    def tasks(self) -> list[dict]:
        return self._request("GET", "/api/tasks")

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
        self.root.title("朋友圈发布站 - Windows Agent")
        self.root.geometry("1180x780")
        self.root.minsize(980, 650)
        self.root.configure(bg=self.colors.paper)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        try:
            import sys

            self.root.iconbitmap(default=sys.executable)
        except Exception:
            pass

        self._closing = False
        self._refreshing = False
        self._status: dict = {}
        self._sources: list[dict] = []
        self._log_dialog = None
        self._facts: dict[str, Any] = {}
        self._callbacks: SimpleQueue[Callable] = SimpleQueue()
        self._configure_styles()
        self._build()
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
            rowheight=30,
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
        outer = tk.Frame(self.root, bg=self.colors.paper, padx=28, pady=22)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.colors.paper)
        header.pack(fill="x", pady=(0, 18))
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
            text="朋友圈发布站",
            bg=self.colors.paper,
            fg=self.colors.ink,
            font=("KaiTi", 34, "bold"),
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            header,
            text="原生桌面控制台\n本地 API 仅供内部通信",
            justify="left",
            bg=self.colors.panel,
            fg=self.colors.ink,
            bd=1,
            relief="solid",
            padx=14,
            pady=9,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", anchor="n")

        rule = tk.Frame(outer, height=2, bg=self.colors.ink)
        rule.pack(fill="x", pady=(0, 16))

        body = tk.PanedWindow(
            outer,
            orient="horizontal",
            sashwidth=8,
            bg=self.colors.paper,
            bd=0,
        )
        body.pack(fill="both", expand=True)
        left = self._panel(body, width=330)
        right = self._panel(body)
        body.add(left, minsize=300, width=340)
        body.add(right, minsize=590)
        self._build_status(left)
        self._build_workspace(right)

    def _panel(self, parent, *, width: int | None = None):
        frame = self.tk.Frame(
            parent,
            bg=self.colors.panel,
            bd=1,
            relief="solid",
            padx=18,
            pady=16,
            width=width,
        )
        if width:
            frame.pack_propagate(False)
        return frame

    def _build_status(self, parent) -> None:
        tk = self.tk
        tk.Label(
            parent,
            text="本机状态",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 22, "bold"),
        ).pack(anchor="w", pady=(0, 12))
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
            row.pack(fill="x", pady=1)
            tk.Label(
                row,
                text=label,
                bg=self.colors.panel,
                fg=self.colors.ink,
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", anchor="n", pady=5)
            value = tk.Label(
                row,
                text="-",
                bg=self.colors.panel,
                fg=self.colors.muted,
                justify="right",
                wraplength=190,
                font=("Microsoft YaHei UI", 9),
            )
            value.pack(side="right", anchor="n", pady=5)
            self._facts[key] = value
            tk.Frame(row, height=1, bg=self.colors.soft).pack(
                side="bottom", fill="x"
            )

        buttons = tk.Frame(parent, bg=self.colors.panel)
        buttons.pack(fill="x", pady=(14, 8))
        self._button(buttons, "环境预检", self.preflight).grid(row=0, column=0, padx=(0, 6), pady=4)
        self._button(buttons, "重新识别", self.identify, alt=True).grid(row=0, column=1, padx=6, pady=4)
        self._button(buttons, "刷新", self.refresh, alt=True).grid(row=1, column=0, padx=(0, 6), pady=4)
        self._button(buttons, "安全退出 Agent", self.shutdown, danger=True).grid(row=1, column=1, padx=6, pady=4)
        self._button(buttons, "查看错误日志", self.open_logs, alt=True).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, 6),
            pady=4,
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
        self.notice_label.pack(fill="x", pady=(8, 0))

    def _build_workspace(self, parent) -> None:
        tk = self.tk
        source_head = tk.Frame(parent, bg=self.colors.panel)
        source_head.pack(fill="x")
        tk.Label(
            source_head,
            text="内容数据源",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 22, "bold"),
        ).pack(side="left")
        self._button(source_head, "+ 添加来源", self.open_source).pack(side="right")

        source_tools = tk.Frame(parent, bg=self.colors.panel)
        source_tools.pack(fill="x", pady=(8, 8))
        self._button(source_tools, "测试连接", self.test_source, alt=True).pack(side="left", padx=(0, 6))
        self._button(source_tools, "编辑 URL / 凭据", self.edit_source, alt=True).pack(side="left", padx=6)
        self._button(source_tools, "删除", self.delete_source, danger=True).pack(side="left", padx=6)

        source_columns = ("name", "state", "account", "url")
        self.source_tree = self.ttk.Treeview(
            parent,
            columns=source_columns,
            show="headings",
            height=7,
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

        task_head = tk.Frame(parent, bg=self.colors.panel)
        task_head.pack(fill="x", pady=(18, 8))
        tk.Label(
            task_head,
            text="最近任务",
            bg=self.colors.panel,
            fg=self.colors.ink,
            font=("KaiTi", 20, "bold"),
        ).pack(side="left")
        self._button(task_head, "刷新任务", self.refresh, alt=True).pack(side="right")

        task_columns = ("source", "task", "state", "attempt", "updated")
        self.task_tree = self.ttk.Treeview(
            parent,
            columns=task_columns,
            show="headings",
            height=9,
            style="Agent.Treeview",
        )
        for column, title, width in (
            ("source", "来源", 130),
            ("task", "任务 ID", 230),
            ("state", "状态", 100),
            ("attempt", "尝试", 60),
            ("updated", "更新时间", 160),
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
            padx=12,
            pady=7,
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
            self._render_status()
            self._render_sources()
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
        self.task_tree.delete(*self.task_tree.get_children())
        for index, task in enumerate(tasks):
            self.task_tree.insert(
                "",
                "end",
                iid=f"task-{index}",
                values=(
                    task.get("source_id", "-"),
                    task.get("task_id", "-"),
                    task.get("state", "-"),
                    task.get("attempt", "-"),
                    task.get("updated_at", "-"),
                ),
            )

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
        from tkinter import messagebox

        if not messagebox.askyesno(
            "安全退出 Agent",
            "确定停止 Agent？\n\n已领取的发布任务执行期间会拒绝退出；微信不会被关闭。",
            parent=self.root,
        ):
            return
        self.notice("正在停止 Agent；识别卡住时最多等待 8 秒...", "info")

        def done(_result):
            self._closing = True
            self.notice("退出请求已接受，可以关闭控制台", "ok")
            self.root.after(700, self.root.destroy)

        self._async(self.client.shutdown, done)

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

    def _source_dialog(self, source: dict | None) -> None:
        tk = self.tk
        from tkinter import messagebox
        from tkinter import ttk

        dialog = tk.Toplevel(self.root)
        dialog.title("编辑数据源" if source else "添加数据源")
        dialog.geometry("690x610")
        dialog.minsize(620, 560)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.colors.paper, padx=24, pady=20)

        values = {
            "id": tk.StringVar(value=(source or {}).get("id", "")),
            "name": tk.StringVar(value=(source or {}).get("name", "")),
            "url": tk.StringVar(value=(source or {}).get("baseUrl", "")),
            "account": tk.StringVar(value=(source or {}).get("accountKey", "wechat-main")),
            "weight": tk.StringVar(value=str((source or {}).get("weight", 1))),
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

        tk.Label(dialog, text="认证类型", bg=self.colors.paper, fg=self.colors.ink, font=("Microsoft YaHei UI", 9, "bold")).grid(row=6, column=0, sticky="w", padx=(0, 14), pady=7)
        auth_box = ttk.Combobox(dialog, textvariable=values["auth"], values=("api_key_header", "bearer"), state="readonly", style="Agent.TCombobox")
        auth_box.grid(row=6, column=1, sticky="ew", pady=7, ipady=4)
        field(7, "Header 名", values["header"])
        field(8, "凭据（留空保持原值）", values["credential"], secret=True)

        tk.Label(dialog, text="媒体域名白名单", bg=self.colors.paper, fg=self.colors.ink, font=("Microsoft YaHei UI", 9, "bold")).grid(row=9, column=0, sticky="nw", padx=(0, 14), pady=7)
        hosts = tk.Text(dialog, height=4, bg=self.colors.panel, fg=self.colors.ink, relief="solid", bd=1, font=("Consolas", 9))
        hosts.grid(row=9, column=1, sticky="nsew", pady=7)
        hosts.insert("1.0", "\n".join((source or {}).get("allowedHosts", [])))

        checks = tk.Frame(dialog, bg=self.colors.paper)
        checks.grid(row=10, column=1, sticky="w", pady=7)
        tk.Checkbutton(checks, text="启用来源", variable=values["enabled"], bg=self.colors.paper, activebackground=self.colors.paper, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 18))
        tk.Checkbutton(checks, text="允许私网媒体", variable=values["private"], bg=self.colors.paper, activebackground=self.colors.paper, font=("Microsoft YaHei UI", 9)).pack(side="left")

        actions = tk.Frame(dialog, bg=self.colors.paper)
        actions.grid(row=11, column=0, columnspan=2, sticky="e", pady=(16, 0))
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
        dialog.rowconfigure(9, weight=1)

    def _selected_source_id(self) -> str | None:
        selected = self.source_tree.selection() if hasattr(self, "source_tree") else ()
        return selected[0] if selected else None

    def _selected_source(self) -> dict | None:
        source_id = self._selected_source_id()
        return next((item for item in self._sources if item.get("id") == source_id), None)

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
            "无法启动朋友圈发布站",
            "本机 Agent 未能在 15 秒内启动。请检查 agent.log 或重新安装。",
            parent=root,
        )
        root.destroy()
        return 1
    return NativeAdminWindow(client).run()
