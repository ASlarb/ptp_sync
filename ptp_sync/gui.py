"""Tkinter desktop interface for hardware and software PTP workflows."""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import asdict, is_dataclass
from typing import Any

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .gui_model import (
    LogSample,
    SoftwareSettings,
    build_software_command,
    parse_log_sample,
)
from .hardware import open_backend
from .hardware.models import HardwareError, HardwareRequest


class OffsetChart(ttk.Frame):
    """Small dependency-free offset chart backed by a Tk Canvas."""

    def __init__(self, parent: tk.Misc, max_samples: int = 180) -> None:
        super().__init__(parent)
        self.samples: deque[LogSample] = deque(maxlen=max_samples)
        self.canvas = tk.Canvas(
            self,
            background="#15191f",
            highlightthickness=0,
            height=270,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

    def clear(self) -> None:
        self.samples.clear()
        self.redraw()

    def append(self, sample: LogSample) -> None:
        self.samples.append(sample)
        self.redraw()

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 180)
        left, right, top, bottom = 66, 18, 18, 38
        plot_width = width - left - right
        plot_height = height - top - bottom
        canvas.create_rectangle(
            left, top, width - right, height - bottom, outline="#3a414b"
        )
        if not self.samples:
            canvas.create_text(
                width / 2,
                height / 2,
                text="等待 slave offset 样本",
                fill="#8b949e",
                font=("TkDefaultFont", 11),
            )
            return

        values = [
            value
            for sample in self.samples
            for value in (sample.offset_us, sample.filtered_us)
        ]
        scale = max(10.0, max(abs(value) for value in values) * 1.15)
        for fraction in (-1.0, -0.5, 0.0, 0.5, 1.0):
            y = top + (1.0 - (fraction + 1.0) / 2.0) * plot_height
            color = "#586069" if fraction == 0 else "#2d333b"
            canvas.create_line(left, y, width - right, y, fill=color)
            canvas.create_text(
                left - 8,
                y,
                text=f"{fraction * scale:+.0f}",
                fill="#aab2bd",
                anchor="e",
            )
        canvas.create_text(
            12,
            top + plot_height / 2,
            text="µs",
            fill="#aab2bd",
            angle=90,
        )

        count = len(self.samples)
        x_step = plot_width / max(1, count - 1)

        def points(attribute: str) -> list[float]:
            result: list[float] = []
            for index, sample in enumerate(self.samples):
                value = float(getattr(sample, attribute))
                x = left + index * x_step
                y = top + ((scale - value) / (2.0 * scale)) * plot_height
                result.extend((x, y))
            return result

        if count == 1:
            sample = self.samples[-1]
            filtered_y = (
                top + ((scale - sample.filtered_us) / (2.0 * scale)) * plot_height
            )
            offset_y = top + (
                (scale - sample.offset_us) / (2.0 * scale)
            ) * plot_height
            canvas.create_oval(
                left - 4,
                filtered_y - 4,
                left + 4,
                filtered_y + 4,
                fill="#ffb86c",
                outline="",
            )
            canvas.create_oval(
                left - 2,
                offset_y - 2,
                left + 2,
                offset_y + 2,
                fill="#4cc9f0",
                outline="",
            )
        else:
            canvas.create_line(
                *points("offset_us"), fill="#4cc9f0", width=1.5, smooth=False
            )
            canvas.create_line(
                *points("filtered_us"), fill="#ffb86c", width=2.0, smooth=False
            )
        canvas.create_text(
            left,
            height - 16,
            text="offset",
            fill="#4cc9f0",
            anchor="w",
        )
        canvas.create_text(
            left + 72,
            height - 16,
            text="filtered",
            fill="#ffb86c",
            anchor="w",
        )
        canvas.create_text(
            width - right,
            height - 16,
            text=f"最近 {count} 个样本",
            fill="#8b949e",
            anchor="e",
        )


class PtpGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.hardware_buttons: list[ttk.Button] = []
        self.hardware_busy = False
        self.hardware_managed_active = False
        self.software_process: subprocess.Popen[str] | None = None
        self.active_software_settings: SoftwareSettings | None = None
        self.software_stopping = False
        self.close_when_software_exits = False

        root.title("PTP Sync Control Center")
        root.geometry("1180x820")
        root.minsize(980, 680)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_style()
        self._build_header()
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_overview_tab()
        self._build_hardware_tab()
        self._build_software_tab()
        root.after(100, self._poll_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
        style.configure("Status.TLabel", font=("TkDefaultFont", 10, "bold"))
        style.configure("Section.TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
        style.configure("Danger.TButton", foreground="#a40000")

    def _build_header(self) -> None:
        frame = ttk.Frame(self.root, padding=(14, 10))
        frame.pack(fill="x")
        ttk.Label(frame, text="PTP Sync Control Center", style="Title.TLabel").pack(
            side="left"
        )
        admin = _is_administrator()
        text = (
            "管理员/root：是"
            if admin
            else "管理员/root：否（apply/restore 将失败）"
        )
        self.admin_label = ttk.Label(frame, text=text, style="Status.TLabel")
        self.admin_label.pack(side="right")

    def _build_overview_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(tab, text="概览")
        text = (
            "硬件 PTP\n"
            "  • Linux：ptp4l 驱动 NIC PHC，phc2sys 同步系统时钟。\n"
            "  • Windows：NDIS hardware timestamp + W32Time PtpClient。\n"
            "  • 推荐顺序：Detect → Plan → Apply → Status；需要撤销时 Restore。\n\n"
            "软件回退\n"
            "  • 启动 Python master/slave，并实时显示日志、offset 和滤波曲线。\n"
            "  • 默认不修改系统时间；slave 勾选“应用系统时钟”后才会纠时。\n"
            "  • 软件时间戳不能替代 NIC hardware timestamp。\n\n"
            "安全提示\n"
            "  • Apply 可能停止时间服务、接管 CLOCK_REALTIME 或重启 Windows 网卡。\n"
            "  • Windows 网卡重启会中断 RDP/SSH/WinRM，请准备带外控制台。\n"
            "  • 软件模式和硬件模式不能同时绑定标准 PTP 端口或调整同一时钟。"
        )
        label = ttk.Label(tab, text=text, justify="left", anchor="nw")
        label.pack(fill="x")
        ttk.Separator(tab).pack(fill="x", pady=18)
        ttk.Label(
            tab,
            text="详细部署、权限、硬件前置条件和故障排查请查看 ptp_sync/README.md。",
        ).pack(anchor="w")

    def _build_hardware_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="硬件 PTP")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        controls = ttk.LabelFrame(
            tab, text="配置", style="Section.TLabelframe", padding=10
        )
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.hw_backend = tk.StringVar(value="auto")
        self.hw_role = tk.StringVar(value="slave")
        self.hw_interface = tk.StringVar()
        self.hw_masters = tk.StringVar()
        self.hw_domain = tk.StringVar(value="0")
        self.hw_utc_offset = tk.StringVar()
        self.hw_state_file = tk.StringVar()

        rows = [
            ("后端", self.hw_backend, ("auto", "linuxptp", "windows-ptp")),
            ("角色", self.hw_role, ("slave", "master")),
        ]
        row = 0
        for label, variable, values in rows:
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Combobox(
                controls,
                textvariable=variable,
                values=values,
                state="readonly",
                width=22,
            ).grid(row=row, column=1, sticky="ew", pady=4)
            row += 1
        for label, variable in (
            ("接口/NIC", self.hw_interface),
            ("GM IPv4（逗号分隔）", self.hw_masters),
            ("Domain", self.hw_domain),
            ("TAI−UTC offset", self.hw_utc_offset),
            ("State file（可选）", self.hw_state_file),
        ):
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(controls, textvariable=variable, width=24).grid(
                row=row, column=1, sticky="ew", pady=4
            )
            row += 1
        controls.columnconfigure(1, weight=1)
        ttk.Separator(controls).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1
        for label, action in (
            ("1. Detect", "detect"),
            ("2. Plan", "plan"),
            ("3. Apply", "apply"),
            ("4. Status", "status"),
            ("5. Restore", "restore"),
        ):
            options = (
                {"style": "Danger.TButton"}
                if action in ("apply", "restore")
                else {}
            )
            button = ttk.Button(
                controls,
                text=label,
                command=lambda selected=action: self._hardware_action(selected),
                **options,
            )
            button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
            self.hardware_buttons.append(button)
            row += 1

        result = ttk.LabelFrame(
            tab, text="结果", style="Section.TLabelframe", padding=8
        )
        result.grid(row=0, column=1, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(1, weight=1)
        result.rowconfigure(2, weight=1)
        self.hw_summary = tk.StringVar(value="尚未执行硬件操作")
        ttk.Label(result, textvariable=self.hw_summary, style="Status.TLabel").grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )
        self.hw_tree = ttk.Treeview(
            result,
            columns=("kind", "name", "status", "detail"),
            show="headings",
            height=12,
        )
        for column, heading, width in (
            ("kind", "类型", 90),
            ("name", "项目", 180),
            ("status", "状态", 80),
            ("detail", "说明", 520),
        ):
            self.hw_tree.heading(column, text=heading)
            self.hw_tree.column(column, width=width, anchor="w")
        self.hw_tree.tag_configure("ok", foreground="#067d17")
        self.hw_tree.tag_configure("fail", foreground="#b00020")
        self.hw_tree.tag_configure("warning", foreground="#9a6700")
        self.hw_tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(
            result, orient="vertical", command=self.hw_tree.yview
        )
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self.hw_tree.configure(yscrollcommand=tree_scroll.set)
        self.hw_raw = scrolledtext.ScrolledText(
            result, height=13, wrap="none", font=("TkFixedFont", 9)
        )
        self.hw_raw.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))

    def _build_software_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="软件 PTP")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        controls = ttk.LabelFrame(
            tab, text="进程配置", style="Section.TLabelframe", padding=10
        )
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.sw_role = tk.StringVar(value="slave")
        self.sw_bind = tk.StringVar(value="0.0.0.0")
        self.sw_interface = tk.StringVar()
        self.sw_peer = tk.StringVar()
        self.sw_master = tk.StringVar()
        self.sw_domain = tk.StringVar(value="0")
        self.sw_event_port = tk.StringVar(value="31900")
        self.sw_general_port = tk.StringVar(value="32000")
        self.sw_peer_event_port = tk.StringVar()
        self.sw_peer_general_port = tk.StringVar()
        self.sw_interval = tk.StringVar(value="1.0")
        self.sw_duration = tk.StringVar()
        self.sw_threshold = tk.StringVar(value="500")
        self.sw_warmup = tk.StringVar(value="4")
        self.sw_window = tk.StringVar(value="8")
        self.sw_apply = tk.BooleanVar(value=False)
        self.sw_multicast = tk.BooleanVar(value=False)
        self.sw_standard = tk.BooleanVar(value=False)
        self.sw_verbose = tk.BooleanVar(value=False)
        self.sw_autoscroll = tk.BooleanVar(value=True)

        row = 0
        ttk.Label(controls, text="角色").grid(row=row, column=0, sticky="w", pady=3)
        role = ttk.Combobox(
            controls,
            textvariable=self.sw_role,
            values=("master", "slave"),
            state="readonly",
            width=21,
        )
        role.grid(row=row, column=1, sticky="ew", pady=3)
        role.bind("<<ComboboxSelected>>", lambda _event: self._update_software_role())
        row += 1
        self.software_entries: dict[str, ttk.Entry] = {}
        fields = (
            ("bind", "本地 Bind", self.sw_bind),
            ("interface", "组播接口 IP", self.sw_interface),
            ("peer", "Master 的 peer", self.sw_peer),
            ("master", "Slave 的 master", self.sw_master),
            ("domain", "Domain", self.sw_domain),
            ("event", "Event port", self.sw_event_port),
            ("general", "General port", self.sw_general_port),
            ("peer_event", "Peer event port", self.sw_peer_event_port),
            ("peer_general", "Peer general port", self.sw_peer_general_port),
            ("interval", "Sync interval", self.sw_interval),
            ("duration", "Duration（可选）", self.sw_duration),
            ("threshold", "Step threshold µs", self.sw_threshold),
            ("warmup", "Warmup", self.sw_warmup),
            ("window", "Median window", self.sw_window),
        )
        for key, label, variable in fields:
            ttk.Label(controls, text=label).grid(
                row=row, column=0, sticky="w", pady=3
            )
            entry = ttk.Entry(controls, textvariable=variable, width=23)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            self.software_entries[key] = entry
            row += 1
        for text, variable in (
            ("应用系统时钟（slave）", self.sw_apply),
            ("Multicast", self.sw_multicast),
            ("标准端口 319/320", self.sw_standard),
            ("Verbose", self.sw_verbose),
        ):
            checkbutton = ttk.Checkbutton(controls, text=text, variable=variable)
            checkbutton.grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2
            )
            if variable is self.sw_apply:
                self.sw_apply_check = checkbutton
            row += 1
        controls.columnconfigure(1, weight=1)
        button_frame = ttk.Frame(controls)
        button_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        button_frame.columnconfigure(0, weight=1)
        button_frame.columnconfigure(1, weight=1)
        self.sw_start = ttk.Button(
            button_frame, text="启动", command=self._start_software
        )
        self.sw_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.sw_stop = ttk.Button(
            button_frame, text="停止", command=self._stop_software, state="disabled"
        )
        self.sw_stop.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        monitor = ttk.Frame(tab)
        monitor.grid(row=0, column=1, sticky="nsew")
        monitor.columnconfigure(0, weight=1)
        monitor.rowconfigure(1, weight=1)
        monitor.rowconfigure(3, weight=1)
        metric_frame = ttk.LabelFrame(
            monitor, text="实时状态", style="Section.TLabelframe", padding=8
        )
        metric_frame.grid(row=0, column=0, sticky="ew")
        metric_frame.columnconfigure((0, 1, 2, 3), weight=1)
        self.sw_process_status = tk.StringVar(value="未运行")
        self.sw_latest_offset = tk.StringVar(value="—")
        self.sw_latest_filtered = tk.StringVar(value="—")
        self.sw_latest_delay = tk.StringVar(value="—")
        for column, (label, variable) in enumerate(
            (
                ("进程", self.sw_process_status),
                ("Offset", self.sw_latest_offset),
                ("Filtered", self.sw_latest_filtered),
                ("Delay", self.sw_latest_delay),
            )
        ):
            ttk.Label(metric_frame, text=label).grid(row=0, column=column)
            ttk.Label(
                metric_frame, textvariable=variable, style="Status.TLabel"
            ).grid(row=1, column=column, padx=8)

        self.offset_chart = OffsetChart(monitor)
        self.offset_chart.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        log_header = ttk.Frame(monitor)
        log_header.grid(row=2, column=0, sticky="ew")
        ttk.Label(log_header, text="进程日志", style="Status.TLabel").pack(side="left")
        ttk.Checkbutton(
            log_header, text="自动滚动", variable=self.sw_autoscroll
        ).pack(side="right")
        ttk.Button(log_header, text="清空", command=self._clear_software_log).pack(
            side="right", padx=8
        )
        self.sw_log = scrolledtext.ScrolledText(
            monitor, height=15, wrap="none", font=("TkFixedFont", 9)
        )
        self.sw_log.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        self._update_software_role()

    def _hardware_request(self, assume_yes: bool = False) -> HardwareRequest:
        domain = int(self.hw_domain.get().strip())
        if not 0 <= domain <= 127:
            raise ValueError("domain 必须在 0..127")
        raw_offset = self.hw_utc_offset.get().strip()
        masters = [
            value
            for value in re.split(r"[\s,;]+", self.hw_masters.get().strip())
            if value
        ]
        return HardwareRequest(
            backend=self.hw_backend.get(),
            role=self.hw_role.get(),
            interface=self.hw_interface.get().strip() or None,
            masters=masters,
            domain=domain,
            utc_offset=int(raw_offset) if raw_offset else None,
            state_file=self.hw_state_file.get().strip() or None,
            assume_yes=assume_yes,
        )

    def _hardware_action(self, action: str) -> None:
        if action in ("apply", "restore") and self._software_clock_conflict():
            messagebox.showerror(
                "模式冲突",
                "软件 PTP 正在使用标准端口或调整系统时钟。请先停止软件进程。",
            )
            return
        if action in ("apply", "restore"):
            message = (
                "Apply 可能停止时间服务、调整系统时钟并重启 Windows 网卡。"
                if action == "apply"
                else "Restore 将停止本工具服务并恢复事务快照。"
            )
            if not messagebox.askyesno("确认系统变更", message + "\n\n是否继续？"):
                return
        try:
            request = self._hardware_request(assume_yes=action == "apply")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self._set_hardware_busy(True)
        self.hw_summary.set(f"正在执行 {action}…")
        self.hw_tree.delete(*self.hw_tree.get_children())
        self._set_text(self.hw_raw, "")
        threading.Thread(
            target=self._hardware_worker,
            args=(action, request),
            daemon=False,
        ).start()

    def _hardware_worker(self, action: str, request: HardwareRequest) -> None:
        try:
            backend = open_backend(request.backend)
            result = getattr(backend, action)(request)
            self.events.put(("hardware_result", (action, result)))
        except (HardwareError, OSError, ValueError) as exc:
            self.events.put(("hardware_error", (action, str(exc))))
        except BaseException as exc:
            self.events.put(
                ("hardware_error", (action, f"{type(exc).__name__}: {exc}"))
            )

    def _show_hardware_result(self, action: str, result: Any) -> None:
        payload = asdict(result) if is_dataclass(result) else dict(result)
        success = payload.get(
            "supported", payload.get("applicable", payload.get("healthy", False))
        )
        self.hw_summary.set(
            f"{action} 完成：{'通过/健康' if success else '未通过/未锁定'}"
        )
        details = [str(value) for value in payload.get("details", [])]
        if action == "restore" and success:
            self.hardware_managed_active = False
        elif action == "apply" and success:
            self.hardware_managed_active = True
        elif action == "status":
            self.hardware_managed_active = bool(success) or any(
                "ptp-sync-" in detail and "active" in detail
                or "W32Time running: True" in detail
                for detail in details
            )
        self.hw_tree.delete(*self.hw_tree.get_children())
        for check in payload.get("checks", []):
            ok = bool(check.get("ok"))
            self.hw_tree.insert(
                "",
                "end",
                values=(
                    "检查",
                    check.get("name", ""),
                    "OK" if ok else "FAIL",
                    check.get("detail", ""),
                ),
                tags=("ok" if ok else "fail",),
            )
        for change in payload.get("changes", []):
            disruptive = bool(change.get("disruptive"))
            self.hw_tree.insert(
                "",
                "end",
                values=(
                    "变更",
                    change.get("target", ""),
                    change.get("action", ""),
                    change.get("detail", ""),
                ),
                tags=("warning" if disruptive else "",),
            )
        for detail in payload.get("details", []):
            self.hw_tree.insert(
                "", "end", values=("状态", "", "", detail)
            )
        for name, value in payload.get("metrics", {}).items():
            self.hw_tree.insert(
                "", "end", values=("指标", name, "", str(value))
            )
        for warning in payload.get("warnings", []):
            self.hw_tree.insert(
                "",
                "end",
                values=("警告", "", "WARN", warning),
                tags=("warning",),
            )
        self._set_text(
            self.hw_raw,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )
        self._set_hardware_busy(False)

    def _set_hardware_busy(self, busy: bool) -> None:
        self.hardware_busy = busy
        state = "disabled" if busy else "normal"
        for button in self.hardware_buttons:
            button.configure(state=state)

    def _update_software_role(self) -> None:
        master_mode = self.sw_role.get() == "master"
        self.software_entries["peer"].configure(
            state="normal" if master_mode else "disabled"
        )
        self.software_entries["interval"].configure(
            state="normal" if master_mode else "disabled"
        )
        for key in ("master", "threshold", "warmup", "window"):
            self.software_entries[key].configure(
                state="disabled" if master_mode else "normal"
            )
        self.sw_apply_check.configure(state="disabled" if master_mode else "normal")

    def _software_settings(self) -> SoftwareSettings:
        return SoftwareSettings(
            role=self.sw_role.get(),
            bind=self.sw_bind.get().strip(),
            interface=self.sw_interface.get().strip() or None,
            peer=self.sw_peer.get().strip() or None,
            master=self.sw_master.get().strip() or None,
            domain=int(self.sw_domain.get().strip()),
            event_port=int(self.sw_event_port.get().strip()),
            general_port=int(self.sw_general_port.get().strip()),
            peer_event_port=_optional_int(self.sw_peer_event_port.get()),
            peer_general_port=_optional_int(self.sw_peer_general_port.get()),
            interval=float(self.sw_interval.get().strip()),
            duration=_optional_float(self.sw_duration.get()),
            apply_clock=bool(self.sw_apply.get()),
            step_threshold_us=float(self.sw_threshold.get().strip()),
            warmup=int(self.sw_warmup.get().strip()),
            window=int(self.sw_window.get().strip()),
            multicast=bool(self.sw_multicast.get()),
            standard_ports=bool(self.sw_standard.get()),
            verbose=bool(self.sw_verbose.get()),
        )

    def _start_software(self) -> None:
        if self.software_process is not None and self.software_process.poll() is None:
            messagebox.showinfo("进程正在运行", "请先停止当前软件 PTP 进程。")
            return
        try:
            settings = self._software_settings()
            command = build_software_command(settings)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if self.hardware_busy:
            messagebox.showerror(
                "硬件操作进行中", "请等待当前 Detect/Plan/Apply/Status/Restore 完成。"
            )
            return
        if self.hardware_managed_active and (
            settings.apply_clock or settings.standard_ports
        ):
            messagebox.showerror(
                "模式冲突",
                "硬件 PTP 正在运行。不能启动会调整系统时钟或占用 319/320 "
                "的软件下载模式；请先在硬件页执行 Restore。",
            )
            return
        if settings.apply_clock and not messagebox.askyesno(
            "确认纠时",
            "将允许软件 slave 修改系统时间。请确认其他校时服务已停止。\n\n是否继续？",
        ):
            return
        self._clear_software_log()
        self.offset_chart.clear()
        self.sw_latest_offset.set("—")
        self.sw_latest_filtered.set("—")
        self.sw_latest_delay.set("—")
        self._append_software_log("$ " + shlex.join(command) + "\n")
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        popen_options: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "env": environment,
        }
        if sys.platform == "win32":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **popen_options)
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.software_process = process
        self.active_software_settings = settings
        self.software_stopping = False
        self.sw_process_status.set(f"运行中 PID={process.pid}")
        self.sw_start.configure(state="disabled")
        self.sw_stop.configure(state="normal")
        threading.Thread(
            target=self._software_reader, args=(process,), daemon=True
        ).start()

    def _software_reader(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self.events.put(("software_line", line))
        finally:
            code = process.wait()
            self.events.put(("software_exit", (process, code)))

    def _stop_software(self) -> None:
        process = self.software_process
        if process is None or process.poll() is not None:
            return
        self.software_stopping = True
        self.sw_process_status.set("正在停止…")
        try:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
        except (OSError, ValueError):
            process.terminate()
        self.root.after(3000, lambda: self._kill_if_running(process))

    @staticmethod
    def _kill_if_running(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()

    def _software_line(self, line: str) -> None:
        self._append_software_log(line)
        sample = parse_log_sample(line)
        if sample is None:
            return
        self.offset_chart.append(sample)
        self.sw_latest_offset.set(f"{sample.offset_us:+.1f} µs")
        self.sw_latest_filtered.set(f"{sample.filtered_us:+.1f} µs")
        self.sw_latest_delay.set(f"{sample.delay_us:.1f} µs")

    def _software_exit(
        self, process: subprocess.Popen[str], return_code: int
    ) -> None:
        if process is not self.software_process:
            return
        stopped = self.software_stopping
        self.software_process = None
        self.active_software_settings = None
        self.software_stopping = False
        self.sw_process_status.set(
            f"{'已停止' if stopped else '已退出'}，code={return_code}"
        )
        self.sw_start.configure(state="normal")
        self.sw_stop.configure(state="disabled")
        if self.close_when_software_exits:
            self.root.destroy()

    def _software_clock_conflict(self) -> bool:
        process = self.software_process
        settings = self.active_software_settings
        return bool(
            process is not None
            and process.poll() is None
            and settings is not None
            and (settings.apply_clock or settings.standard_ports)
        )

    def _append_software_log(self, text: str) -> None:
        self.sw_log.insert("end", text)
        if self.sw_autoscroll.get():
            self.sw_log.see("end")

    def _clear_software_log(self) -> None:
        self.sw_log.delete("1.0", "end")

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "hardware_result":
                    self._show_hardware_result(*payload)
                elif event == "hardware_error":
                    action, message = payload
                    self.hw_summary.set(f"{action} 失败")
                    self._set_text(self.hw_raw, message)
                    self._set_hardware_busy(False)
                    messagebox.showerror("硬件 PTP 操作失败", message)
                elif event == "software_line":
                    self._software_line(payload)
                elif event == "software_exit":
                    self._software_exit(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _on_close(self) -> None:
        if self.hardware_busy:
            messagebox.showwarning(
                "硬件事务进行中",
                "当前硬件操作尚未完成。为确保事务提交或回滚完整，暂时不能关闭窗口。",
            )
            return
        process = self.software_process
        if process is not None and process.poll() is None:
            if not messagebox.askyesno(
                "退出", "软件 PTP 进程仍在运行。停止进程并退出？"
            ):
                return
            self.close_when_software_exits = True
            self._stop_software()
            return
        self.root.destroy()


def _optional_int(raw: str) -> int | None:
    value = raw.strip()
    return int(value) if value else None


def _optional_float(raw: str) -> float | None:
    value = raw.strip()
    return float(value) if value else None


def _is_administrator() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def launch_gui() -> int:
    root = tk.Tk()
    PtpGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch_gui())

