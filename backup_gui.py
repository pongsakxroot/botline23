#!/usr/bin/env python3
"""
Professional GUI for backup configuration and execution.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from backup_tool import (
    AppConfig,
    DatabaseConfig,
    ScheduleConfig,
    TelegramConfig,
    build_result_message,
    execute_backup_cycle,
    load_config,
    next_daily_run,
    parse_selected_targets,
    send_telegram_message,
)


class TextQueueHandler(logging.Handler):
    def __init__(self, target_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.target_queue = target_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.target_queue.put(self.format(record))


class DatabaseDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, initial: dict | None = None) -> None:
        super().__init__(parent)
        self.title("Database Settings")
        self.resizable(False, False)
        self.result: dict | None = None

        data = initial or {}
        self.var_name = tk.StringVar(value=data.get("name", ""))
        self.var_type = tk.StringVar(value=data.get("type", "mysql"))
        self.var_host = tk.StringVar(value=data.get("host", "127.0.0.1"))
        self.var_port = tk.StringVar(value=str(data.get("port", 3306)))
        self.var_username = tk.StringVar(value=data.get("username", ""))
        self.var_password = tk.StringVar(value=data.get("password", ""))
        self.var_database = tk.StringVar(value=data.get("database", ""))
        self.var_enabled = tk.BooleanVar(value=bool(data.get("enabled", True)))
        self.var_skip_log_tables = tk.BooleanVar(value=bool(data.get("skip_log_tables", False)))
        self.var_exclude_tables = tk.StringVar(value=", ".join(data.get("exclude_tables", [])))
        self.var_extra_args = tk.StringVar(value=", ".join(data.get("extra_args", [])))

        frame = ttk.Frame(self, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        fields = [
            ("Name", self.var_name),
            ("Type", self.var_type),
            ("Host", self.var_host),
            ("Port", self.var_port),
            ("Username", self.var_username),
            ("Password", self.var_password),
            ("Database", self.var_database),
            ("Exclude tables (comma-separated)", self.var_exclude_tables),
            ("Extra args (comma-separated)", self.var_extra_args),
        ]
        for idx, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", pady=4, padx=(0, 8))
            if label == "Type":
                ttk.Combobox(
                    frame,
                    textvariable=var,
                    values=["mysql", "postgresql"],
                    state="readonly",
                    width=35,
                ).grid(row=idx, column=1, sticky="ew", pady=4)
            elif label == "Password":
                ttk.Entry(frame, textvariable=var, show="*", width=38).grid(
                    row=idx, column=1, sticky="ew", pady=4
                )
            else:
                ttk.Entry(frame, textvariable=var, width=38).grid(row=idx, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(frame, text="Enabled", variable=self.var_enabled).grid(
            row=len(fields), column=1, sticky="w", pady=4
        )
        ttk.Checkbutton(
            frame,
            text="Skip log tables (for this DB)",
            variable=self.var_skip_log_tables,
        ).grid(row=len(fields) + 1, column=1, sticky="w", pady=4)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=6)
        ttk.Button(btn_frame, text="Save", command=self.on_save).grid(row=0, column=1, padx=6)

        frame.columnconfigure(1, weight=1)
        self.grab_set()
        self.transient(parent)

    def on_save(self) -> None:
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            messagebox.showerror("Invalid value", "Port must be a number.")
            return

        if not self.var_name.get().strip():
            messagebox.showerror("Invalid value", "Name is required.")
            return
        if not self.var_database.get().strip():
            messagebox.showerror("Invalid value", "Database is required.")
            return

        self.result = {
            "name": self.var_name.get().strip(),
            "type": self.var_type.get().strip(),
            "host": self.var_host.get().strip(),
            "port": port,
            "username": self.var_username.get().strip(),
            "password": self.var_password.get(),
            "database": self.var_database.get().strip(),
            "enabled": bool(self.var_enabled.get()),
            "skip_log_tables": bool(self.var_skip_log_tables.get()),
            "exclude_tables": [
                item.strip() for item in self.var_exclude_tables.get().split(",") if item.strip()
            ],
            "extra_args": [item.strip() for item in self.var_extra_args.get().split(",") if item.strip()],
        }
        self.destroy()


class BackupGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Backup Studio Pro")
        self.geometry("1120x790")
        self.minsize(1040, 720)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.scheduler_thread: threading.Thread | None = None
        self.run_thread: threading.Thread | None = None
        self.scheduler_stop_event = threading.Event()
        self.db_entries: list[dict] = []

        self.var_config_path = tk.StringVar(value="backup_config.json")
        self.var_targets = tk.StringVar(value="")
        self.var_backup_root = tk.StringVar(value="backups")
        self.var_zip_output = tk.BooleanVar(value=True)
        self.var_keep_sql = tk.BooleanVar(value=False)
        self.var_skip_log_tables = tk.BooleanVar(value=True)
        self.var_log_keywords = tk.StringVar(value="log,logs,audit,history,event_log")

        self.var_tg_enabled = tk.BooleanVar(value=False)
        self.var_tg_token = tk.StringVar(value="")
        self.var_tg_chat_id = tk.StringVar(value="")

        self.var_sched_enabled = tk.BooleanVar(value=True)
        self.var_sched_mode = tk.StringVar(value="daily")
        self.var_sched_time = tk.StringVar(value="02:00")
        self.var_sched_interval = tk.StringVar(value="60")
        self.var_sched_timezone = tk.StringVar(value="Asia/Bangkok")

        self.var_progress = tk.DoubleVar(value=0.0)
        self.var_progress_text = tk.StringVar(value="Ready")

        self.btn_run_now: ttk.Button | None = None
        self.btn_start_auto: ttk.Button | None = None
        self.btn_stop_auto: ttk.Button | None = None
        self.tree_db: ttk.Treeview | None = None
        self.txt_log: tk.Text | None = None

        self.setup_style()
        self.setup_logging()
        self.build_ui()
        self.after(150, self.poll_queues)

    def setup_style(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        if "clam" in available:
            style.theme_use("clam")
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Subtle.TLabel", foreground="#4a4f55")
        style.configure("Accent.TButton", padding=(12, 6))
        style.configure("Treeview", rowheight=26)

    def setup_logging(self) -> None:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        handler = TextQueueHandler(self.log_queue)
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not any(isinstance(h, TextQueueHandler) for h in root.handlers):
            root.addHandler(handler)

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        self.build_header(root)
        self.build_action_bar(root)
        self.build_notebook(root)

    def build_header(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Project Config", padding=10)
        frame.pack(fill="x", pady=(0, 8))

        ttk.Label(frame, text="Config Path").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_config_path, width=75).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(frame, text="Browse", command=self.on_browse_config).grid(row=0, column=2, padx=3)
        ttk.Button(frame, text="Load", command=self.on_load_config).grid(row=0, column=3, padx=3)
        ttk.Button(frame, text="Save", command=self.on_save_config).grid(row=0, column=4, padx=3)
        frame.columnconfigure(1, weight=1)

    def build_action_bar(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Control Center", padding=10)
        frame.pack(fill="x", pady=(0, 8))

        self.btn_run_now = ttk.Button(frame, text="Run Backup Now", style="Accent.TButton", command=self.on_run_now)
        self.btn_run_now.grid(row=0, column=0, padx=4, sticky="w")
        self.btn_start_auto = ttk.Button(
            frame,
            text="Start Auto Backup",
            style="Accent.TButton",
            command=self.on_start_auto,
        )
        self.btn_start_auto.grid(row=0, column=1, padx=4, sticky="w")
        self.btn_stop_auto = ttk.Button(
            frame,
            text="Stop Auto Backup",
            command=self.on_stop_auto,
            state="disabled",
        )
        self.btn_stop_auto.grid(row=0, column=2, padx=4, sticky="w")
        ttk.Button(frame, text="Clear Log", command=self.on_clear_log).grid(row=0, column=3, padx=4, sticky="w")

        ttk.Label(frame, textvariable=self.var_progress_text, style="Subtle.TLabel").grid(
            row=0, column=4, sticky="e", padx=(12, 4)
        )
        progress = ttk.Progressbar(
            frame,
            variable=self.var_progress,
            mode="determinate",
            maximum=100,
            length=260,
        )
        progress.grid(row=0, column=5, sticky="e", padx=4)
        frame.columnconfigure(4, weight=1)

    def build_notebook(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        tab_general = ttk.Frame(notebook, padding=12)
        tab_databases = ttk.Frame(notebook, padding=12)
        tab_schedule = ttk.Frame(notebook, padding=12)
        tab_telegram = ttk.Frame(notebook, padding=12)
        tab_monitor = ttk.Frame(notebook, padding=12)

        notebook.add(tab_general, text="General")
        notebook.add(tab_databases, text="Databases")
        notebook.add(tab_schedule, text="Schedule")
        notebook.add(tab_telegram, text="Telegram")
        notebook.add(tab_monitor, text="Monitor")

        self.build_general_tab(tab_general)
        self.build_databases_tab(tab_databases)
        self.build_schedule_tab(tab_schedule)
        self.build_telegram_tab(tab_telegram)
        self.build_monitor_tab(tab_monitor)

    def build_general_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="General Backup Settings", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        ttk.Label(parent, text="Backup folder").grid(row=1, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.var_backup_root, width=40).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Checkbutton(parent, text="Zip output files", variable=self.var_zip_output).grid(
            row=2, column=0, sticky="w", pady=5
        )
        ttk.Checkbutton(parent, text="Keep .sql after zip", variable=self.var_keep_sql).grid(
            row=2, column=1, sticky="w", pady=5
        )
        ttk.Checkbutton(
            parent,
            text="Skip log tables automatically",
            variable=self.var_skip_log_tables,
        ).grid(row=3, column=0, sticky="w", pady=5)

        ttk.Label(parent, text="Log table keywords").grid(row=3, column=1, sticky="e")
        ttk.Entry(parent, textvariable=self.var_log_keywords, width=50).grid(row=3, column=2, sticky="ew", padx=8)
        ttk.Label(parent, text="Targets (optional)").grid(row=4, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.var_targets, width=70).grid(row=4, column=1, columnspan=2, sticky="ew", padx=8)
        ttk.Label(parent, text="Example: main-mysql,main-postgres", style="Subtle.TLabel").grid(
            row=5, column=1, columnspan=2, sticky="w", pady=(4, 0)
        )
        parent.columnconfigure(2, weight=1)

    def build_databases_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Database Connections", style="Header.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )

        columns = ("name", "type", "host", "database", "skip_logs", "exclude", "enabled")
        self.tree_db = ttk.Treeview(parent, columns=columns, show="headings", height=14)
        specs = [
            ("name", 160, "Name"),
            ("type", 100, "Type"),
            ("host", 180, "Host"),
            ("database", 180, "Database"),
            ("skip_logs", 90, "Skip log"),
            ("exclude", 180, "Exclude tables"),
            ("enabled", 80, "Enabled"),
        ]
        for key, width, title in specs:
            self.tree_db.heading(key, text=title)
            self.tree_db.column(key, width=width, anchor="w")

        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree_db.yview)
        self.tree_db.configure(yscrollcommand=scrollbar.set)
        self.tree_db.grid(row=1, column=0, columnspan=3, sticky="nsew")
        scrollbar.grid(row=1, column=3, sticky="ns")

        ttk.Button(parent, text="Add", command=self.on_add_db).grid(row=2, column=0, sticky="w", pady=8)
        ttk.Button(parent, text="Edit", command=self.on_edit_db).grid(row=2, column=1, sticky="w", pady=8)
        ttk.Button(parent, text="Remove", command=self.on_remove_db).grid(row=2, column=2, sticky="w", pady=8)
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(2, weight=1)

    def build_schedule_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Schedule Settings", style="Header.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(parent, text="Enable schedule", variable=self.var_sched_enabled).grid(
            row=1, column=0, sticky="w", pady=6
        )
        ttk.Label(parent, text="Mode").grid(row=1, column=1, sticky="e")
        ttk.Combobox(
            parent,
            textvariable=self.var_sched_mode,
            values=["daily", "interval"],
            state="readonly",
            width=16,
        ).grid(row=1, column=2, sticky="w", padx=6)

        ttk.Label(parent, text="Daily time (HH:MM)").grid(row=2, column=1, sticky="e")
        ttk.Entry(parent, textvariable=self.var_sched_time, width=18).grid(row=2, column=2, sticky="w", padx=6)
        ttk.Label(parent, text="Interval minutes").grid(row=3, column=1, sticky="e")
        ttk.Entry(parent, textvariable=self.var_sched_interval, width=18).grid(row=3, column=2, sticky="w", padx=6)
        ttk.Label(parent, text="Timezone").grid(row=4, column=1, sticky="e")
        ttk.Entry(parent, textvariable=self.var_sched_timezone, width=28).grid(row=4, column=2, sticky="w", padx=6)

    def build_telegram_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Telegram Notification", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        ttk.Checkbutton(parent, text="Enabled", variable=self.var_tg_enabled).grid(row=1, column=0, sticky="w")
        ttk.Label(parent, text="Bot token").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.var_tg_token, width=70).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Label(parent, text="Chat ID").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.var_tg_chat_id, width=30).grid(row=3, column=1, sticky="w", padx=8)
        parent.columnconfigure(1, weight=1)

    def build_monitor_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Execution Monitor", style="Header.TLabel").pack(anchor="w", pady=(0, 10))
        self.txt_log = tk.Text(parent, wrap="word", height=24, font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scrollbar.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def poll_queues(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get()
            if self.txt_log is not None:
                self.txt_log.insert("end", line + "\n")
                self.txt_log.see("end")

        while not self.ui_queue.empty():
            event, payload = self.ui_queue.get()
            if event == "progress":
                percent, text = payload  # type: ignore[misc]
                self.set_progress(float(percent), str(text))
            elif event == "run_done":
                self.toggle_run_buttons(False)
            elif event == "auto_started":
                self.toggle_auto_buttons(True)
            elif event == "auto_stopped":
                self.toggle_auto_buttons(False)

        self.after(150, self.poll_queues)

    def set_progress(self, percent: float, text: str) -> None:
        safe = max(0.0, min(100.0, percent))
        self.var_progress.set(safe)
        self.var_progress_text.set(f"{safe:5.1f}% | {text}")

    def on_clear_log(self) -> None:
        if self.txt_log is not None:
            self.txt_log.delete("1.0", "end")

    def on_browse_config(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Select config file",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=Path(self.var_config_path.get()).name,
        )
        if path:
            self.var_config_path.set(path)

    def refresh_db_tree(self) -> None:
        if self.tree_db is None:
            return
        for item in self.tree_db.get_children():
            self.tree_db.delete(item)
        for idx, db in enumerate(self.db_entries):
            exclude_preview = ",".join(db.get("exclude_tables", []))
            if len(exclude_preview) > 28:
                exclude_preview = exclude_preview[:28] + "..."
            self.tree_db.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    db.get("name", ""),
                    db.get("type", ""),
                    db.get("host", ""),
                    db.get("database", ""),
                    str(bool(db.get("skip_log_tables", False))),
                    exclude_preview,
                    str(bool(db.get("enabled", True))),
                ),
            )

    def selected_db_index(self) -> int | None:
        if self.tree_db is None:
            return None
        selected = self.tree_db.selection()
        if not selected:
            return None
        return int(selected[0])

    def on_add_db(self) -> None:
        dialog = DatabaseDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.db_entries.append(dialog.result)
            self.refresh_db_tree()
            logging.info("Database entry added: %s", dialog.result.get("name", ""))

    def on_edit_db(self) -> None:
        idx = self.selected_db_index()
        if idx is None:
            messagebox.showinfo("No selection", "Please select a database entry first.")
            return
        dialog = DatabaseDialog(self, initial=self.db_entries[idx])
        self.wait_window(dialog)
        if dialog.result:
            self.db_entries[idx] = dialog.result
            self.refresh_db_tree()
            logging.info("Database entry updated: %s", dialog.result.get("name", ""))

    def on_remove_db(self) -> None:
        idx = self.selected_db_index()
        if idx is None:
            messagebox.showinfo("No selection", "Please select a database entry first.")
            return
        removed = self.db_entries.pop(idx)
        self.refresh_db_tree()
        logging.info("Database entry removed: %s", removed.get("name", ""))

    def parse_keywords(self) -> list[str]:
        return [kw.strip() for kw in self.var_log_keywords.get().split(",") if kw.strip()]

    def load_values_from_config(self, cfg: AppConfig) -> None:
        self.var_backup_root.set(cfg.backup_root)
        self.var_zip_output.set(cfg.zip_output)
        self.var_keep_sql.set(cfg.keep_sql_after_zip)
        self.var_skip_log_tables.set(cfg.skip_log_tables)
        self.var_log_keywords.set(",".join(cfg.log_table_keywords))

        self.var_tg_enabled.set(cfg.telegram.enabled)
        self.var_tg_token.set(cfg.telegram.bot_token)
        self.var_tg_chat_id.set(cfg.telegram.chat_id)

        self.var_sched_enabled.set(cfg.schedule.enabled)
        self.var_sched_mode.set(cfg.schedule.mode)
        self.var_sched_time.set(cfg.schedule.time)
        self.var_sched_interval.set(str(cfg.schedule.interval_minutes))
        self.var_sched_timezone.set(cfg.schedule.timezone)

        self.db_entries = [
            {
                "name": db.name,
                "type": db.db_type,
                "host": db.host,
                "port": db.port,
                "username": db.username,
                "password": db.password,
                "database": db.database,
                "enabled": db.enabled,
                "skip_log_tables": bool(db.skip_log_tables),
                "exclude_tables": list(db.exclude_tables),
                "extra_args": list(db.extra_args),
            }
            for db in cfg.databases
        ]
        self.refresh_db_tree()
        logging.info("Config loaded into GUI successfully.")

    def build_config_dict(self) -> dict:
        interval = int(self.var_sched_interval.get().strip() or "60")
        return {
            "backup_root": self.var_backup_root.get().strip() or "backups",
            "zip_output": bool(self.var_zip_output.get()),
            "keep_sql_after_zip": bool(self.var_keep_sql.get()),
            "skip_log_tables": bool(self.var_skip_log_tables.get()),
            "log_table_keywords": self.parse_keywords(),
            "telegram": {
                "enabled": bool(self.var_tg_enabled.get()),
                "bot_token": self.var_tg_token.get().strip(),
                "chat_id": self.var_tg_chat_id.get().strip(),
            },
            "schedule": {
                "enabled": bool(self.var_sched_enabled.get()),
                "mode": self.var_sched_mode.get().strip() or "daily",
                "time": self.var_sched_time.get().strip() or "02:00",
                "interval_minutes": interval,
                "timezone": self.var_sched_timezone.get().strip() or "UTC",
            },
            "databases": self.db_entries,
        }

    def build_app_config(self) -> AppConfig:
        try:
            interval = int(self.var_sched_interval.get().strip() or "60")
        except ValueError as exc:
            raise ValueError("Schedule interval must be a number.") from exc

        databases: list[DatabaseConfig] = []
        for db in self.db_entries:
            try:
                port = int(db.get("port", 0))
            except ValueError as exc:
                raise ValueError(f"Invalid port in database: {db.get('name', '')}") from exc

            databases.append(
                DatabaseConfig(
                    name=db.get("name", "").strip(),
                    db_type=db.get("type", "mysql").strip(),
                    host=db.get("host", "127.0.0.1").strip(),
                    port=port,
                    username=db.get("username", "").strip(),
                    password=db.get("password", ""),
                    database=db.get("database", "").strip(),
                    enabled=bool(db.get("enabled", True)),
                    skip_log_tables=bool(db.get("skip_log_tables", False)),
                    exclude_tables=list(db.get("exclude_tables", [])),
                    extra_args=list(db.get("extra_args", [])),
                )
            )

        return AppConfig(
            backup_root=self.var_backup_root.get().strip() or "backups",
            zip_output=bool(self.var_zip_output.get()),
            keep_sql_after_zip=bool(self.var_keep_sql.get()),
            skip_log_tables=bool(self.var_skip_log_tables.get()),
            log_table_keywords=self.parse_keywords(),
            telegram=TelegramConfig(
                enabled=bool(self.var_tg_enabled.get()),
                bot_token=self.var_tg_token.get().strip(),
                chat_id=self.var_tg_chat_id.get().strip(),
            ),
            schedule=ScheduleConfig(
                enabled=bool(self.var_sched_enabled.get()),
                mode=self.var_sched_mode.get().strip() or "daily",
                time=self.var_sched_time.get().strip() or "02:00",
                interval_minutes=interval,
                timezone=self.var_sched_timezone.get().strip() or "UTC",
            ),
            databases=databases,
        )

    def on_load_config(self) -> None:
        path = Path(self.var_config_path.get().strip() or "backup_config.json")
        if not path.exists():
            messagebox.showerror("Not found", f"Config file not found:\n{path}")
            return
        try:
            cfg = load_config(path)
            self.load_values_from_config(cfg)
            logging.info("Loaded config: %s", path)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))

    def on_save_config(self) -> None:
        path = Path(self.var_config_path.get().strip() or "backup_config.json")
        try:
            data = self.build_config_dict()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logging.info("Saved config: %s", path)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def toggle_run_buttons(self, is_running: bool) -> None:
        if self.btn_run_now is not None:
            self.btn_run_now.configure(state="disabled" if is_running else "normal")
        if self.btn_start_auto is not None:
            self.btn_start_auto.configure(state="disabled" if is_running else "normal")

    def toggle_auto_buttons(self, is_running: bool) -> None:
        if self.btn_start_auto is not None:
            self.btn_start_auto.configure(state="disabled" if is_running else "normal")
        if self.btn_stop_auto is not None:
            self.btn_stop_auto.configure(state="normal" if is_running else "disabled")

    def report_progress(self, percent: float, message: str) -> None:
        self.ui_queue.put(("progress", (percent, message)))
        logging.info("Progress %.0f%% | %s", percent, message)

    def on_run_now(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            messagebox.showinfo("Busy", "Backup is already running.")
            return
        self.toggle_run_buttons(True)
        self.run_thread = threading.Thread(target=self.run_once_worker, daemon=True)
        self.run_thread.start()

    def run_once_worker(self) -> None:
        try:
            cfg = self.build_app_config()
            selected_targets = parse_selected_targets(self.var_targets.get().strip())
            success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(
                cfg,
                selected_targets,
                progress_callback=self.report_progress,
            )
            msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
            logging.info(msg)
            send_telegram_message(cfg.telegram, msg)
        except Exception:
            logging.exception("Backup run failed")
        finally:
            self.ui_queue.put(("run_done", None))

    def on_start_auto(self) -> None:
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            messagebox.showinfo("Already running", "Auto backup is already running.")
            return
        try:
            snapshot_cfg = self.build_app_config()
        except Exception as exc:
            messagebox.showerror("Invalid config", str(exc))
            return
        if not snapshot_cfg.schedule.enabled:
            messagebox.showwarning("Schedule disabled", "Schedule is disabled in settings.")
            return

        self.scheduler_stop_event.clear()
        targets = self.var_targets.get().strip()
        self.scheduler_thread = threading.Thread(
            target=self.auto_worker,
            args=(snapshot_cfg, targets),
            daemon=True,
        )
        self.scheduler_thread.start()
        self.ui_queue.put(("auto_started", None))
        logging.info("Auto backup started.")

    def on_stop_auto(self) -> None:
        self.scheduler_stop_event.set()
        logging.info("Stopping auto backup...")

    def auto_worker(self, snapshot_cfg: AppConfig, targets_text: str) -> None:
        schedule = snapshot_cfg.schedule
        selected_targets = parse_selected_targets(targets_text)
        first_interval_cycle = True

        try:
            while not self.scheduler_stop_event.is_set():
                if schedule.mode == "daily":
                    run_at = next_daily_run(schedule.time, schedule.timezone)
                    self.report_progress(0.0, f"Waiting for daily schedule: {run_at.isoformat()}")
                    now = datetime.now(run_at.tzinfo)
                    wait_seconds = max((run_at - now).total_seconds(), 0)
                    if self.scheduler_stop_event.wait(wait_seconds):
                        break
                elif schedule.mode == "interval":
                    interval_minutes = max(schedule.interval_minutes, 1)
                    if first_interval_cycle:
                        first_interval_cycle = False
                    else:
                        self.report_progress(0.0, f"Waiting next run in {interval_minutes} minute(s)")
                        if self.scheduler_stop_event.wait(interval_minutes * 60):
                            break
                else:
                    raise ValueError("schedule.mode must be 'daily' or 'interval'")

                success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(
                    snapshot_cfg,
                    selected_targets,
                    progress_callback=self.report_progress,
                )
                msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
                logging.info(msg)
                send_telegram_message(snapshot_cfg.telegram, msg)
        except Exception:
            logging.exception("Auto backup crashed")
        finally:
            self.ui_queue.put(("auto_stopped", None))
            self.report_progress(0.0, "Auto backup stopped")


def main() -> int:
    app = BackupGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
