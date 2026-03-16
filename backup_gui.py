#!/usr/bin/env python3
"""
Simple GUI for database backup configuration and execution.
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
        msg = self.format(record)
        self.target_queue.put(msg)


class DatabaseDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, initial: dict | None = None) -> None:
        super().__init__(parent)
        self.title("Database Setting")
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
        self.var_extra_args = tk.StringVar(value=", ".join(data.get("extra_args", [])))

        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")

        fields = [
            ("Name", self.var_name),
            ("Type", self.var_type),
            ("Host", self.var_host),
            ("Port", self.var_port),
            ("Username", self.var_username),
            ("Password", self.var_password),
            ("Database", self.var_database),
            ("Extra args (comma-separated)", self.var_extra_args),
        ]

        for idx, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", pady=3)
            if label == "Type":
                ttk.Combobox(
                    frame,
                    textvariable=var,
                    values=["mysql", "postgresql"],
                    state="readonly",
                    width=32,
                ).grid(row=idx, column=1, sticky="ew", pady=3)
            elif label == "Password":
                ttk.Entry(frame, textvariable=var, width=35, show="*").grid(
                    row=idx, column=1, sticky="ew", pady=3
                )
            else:
                ttk.Entry(frame, textvariable=var, width=35).grid(
                    row=idx, column=1, sticky="ew", pady=3
                )

        ttk.Checkbutton(frame, text="Enabled", variable=self.var_enabled).grid(
            row=len(fields), column=1, sticky="w", pady=3
        )

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(btn_frame, text="Save", command=self.on_save).grid(row=0, column=1, padx=4)

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
            "enabled": self.var_enabled.get(),
            "extra_args": [
                item.strip() for item in self.var_extra_args.get().split(",") if item.strip()
            ],
        }
        self.destroy()


class BackupGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Database Backup Tool (GUI)")
        self.geometry("980x760")

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.scheduler_thread: threading.Thread | None = None
        self.run_thread: threading.Thread | None = None
        self.scheduler_stop_event = threading.Event()
        self.db_entries: list[dict] = []

        self.var_config_path = tk.StringVar(value="backup_config.json")
        self.var_targets = tk.StringVar(value="")
        self.var_backup_root = tk.StringVar(value="backups")
        self.var_zip_output = tk.BooleanVar(value=True)
        self.var_keep_sql = tk.BooleanVar(value=False)

        self.var_tg_enabled = tk.BooleanVar(value=False)
        self.var_tg_token = tk.StringVar(value="")
        self.var_tg_chat_id = tk.StringVar(value="")

        self.var_sched_enabled = tk.BooleanVar(value=True)
        self.var_sched_mode = tk.StringVar(value="daily")
        self.var_sched_time = tk.StringVar(value="02:00")
        self.var_sched_interval = tk.StringVar(value="60")
        self.var_sched_timezone = tk.StringVar(value="Asia/Bangkok")

        self.btn_run_now: ttk.Button | None = None
        self.btn_start_auto: ttk.Button | None = None
        self.btn_stop_auto: ttk.Button | None = None
        self.tree_db: ttk.Treeview | None = None
        self.txt_log: tk.Text | None = None

        self.setup_logging()
        self.build_ui()
        self.after(200, self.poll_log_queue)

    def setup_logging(self) -> None:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        handler = TextQueueHandler(self.log_queue)
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        self.build_config_frame(root)
        self.build_main_settings_frame(root)
        self.build_telegram_frame(root)
        self.build_schedule_frame(root)
        self.build_database_frame(root)
        self.build_action_frame(root)
        self.build_log_frame(root)

    def build_config_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Config File", padding=10)
        frame.pack(fill="x", pady=5)

        ttk.Label(frame, text="Path").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_config_path, width=65).grid(
            row=0, column=1, sticky="ew", padx=5
        )
        ttk.Button(frame, text="Browse", command=self.on_browse_config).grid(row=0, column=2, padx=3)
        ttk.Button(frame, text="Load", command=self.on_load_config).grid(row=0, column=3, padx=3)
        ttk.Button(frame, text="Save", command=self.on_save_config).grid(row=0, column=4, padx=3)
        frame.columnconfigure(1, weight=1)

    def build_main_settings_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Main Settings", padding=10)
        frame.pack(fill="x", pady=5)

        ttk.Label(frame, text="Backup folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_backup_root, width=35).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Zip output", variable=self.var_zip_output).grid(
            row=0, column=2, sticky="w", padx=8
        )
        ttk.Checkbutton(frame, text="Keep .sql after zip", variable=self.var_keep_sql).grid(
            row=0, column=3, sticky="w"
        )

        ttk.Label(frame, text="Targets (optional)").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_targets, width=50).grid(
            row=1, column=1, columnspan=3, sticky="ew"
        )
        ttk.Label(frame, text="ex: main-mysql,main-postgres").grid(row=2, column=1, sticky="w", pady=(2, 0))
        frame.columnconfigure(1, weight=1)

    def build_telegram_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Telegram Notification", padding=10)
        frame.pack(fill="x", pady=5)

        ttk.Checkbutton(frame, text="Enabled", variable=self.var_tg_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Bot token").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_tg_token, width=45).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Label(frame, text="Chat ID").grid(row=1, column=2, sticky="w")
        ttk.Entry(frame, textvariable=self.var_tg_chat_id, width=25).grid(row=1, column=3, sticky="ew", padx=4)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

    def build_schedule_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Schedule", padding=10)
        frame.pack(fill="x", pady=5)

        ttk.Checkbutton(frame, text="Enabled", variable=self.var_sched_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Mode").grid(row=0, column=1, sticky="e")
        ttk.Combobox(
            frame,
            textvariable=self.var_sched_mode,
            values=["daily", "interval"],
            state="readonly",
            width=12,
        ).grid(row=0, column=2, sticky="w", padx=4)

        ttk.Label(frame, text="Daily time (HH:MM)").grid(row=0, column=3, sticky="e")
        ttk.Entry(frame, textvariable=self.var_sched_time, width=12).grid(row=0, column=4, sticky="w", padx=4)

        ttk.Label(frame, text="Interval (min)").grid(row=0, column=5, sticky="e")
        ttk.Entry(frame, textvariable=self.var_sched_interval, width=8).grid(row=0, column=6, sticky="w", padx=4)

        ttk.Label(frame, text="Timezone").grid(row=0, column=7, sticky="e")
        ttk.Entry(frame, textvariable=self.var_sched_timezone, width=18).grid(row=0, column=8, sticky="w", padx=4)

    def build_database_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Databases", padding=10)
        frame.pack(fill="both", pady=5, expand=True)

        columns = ("name", "type", "host", "database", "enabled")
        self.tree_db = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        for col, width in (
            ("name", 180),
            ("type", 110),
            ("host", 180),
            ("database", 180),
            ("enabled", 90),
        ):
            self.tree_db.heading(col, text=col)
            self.tree_db.column(col, width=width, anchor="w")

        self.tree_db.grid(row=0, column=0, columnspan=3, sticky="nsew")
        ttk.Button(frame, text="Add", command=self.on_add_db).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Button(frame, text="Edit", command=self.on_edit_db).grid(row=1, column=1, sticky="w", pady=6)
        ttk.Button(frame, text="Remove", command=self.on_remove_db).grid(row=1, column=2, sticky="w", pady=6)

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def build_action_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Actions", padding=10)
        frame.pack(fill="x", pady=5)

        self.btn_run_now = ttk.Button(frame, text="Run Backup Now", command=self.on_run_now)
        self.btn_run_now.grid(row=0, column=0, padx=4)

        self.btn_start_auto = ttk.Button(frame, text="Start Auto Backup", command=self.on_start_auto)
        self.btn_start_auto.grid(row=0, column=1, padx=4)

        self.btn_stop_auto = ttk.Button(
            frame,
            text="Stop Auto Backup",
            command=self.on_stop_auto,
            state="disabled",
        )
        self.btn_stop_auto.grid(row=0, column=2, padx=4)

    def build_log_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Log", padding=10)
        frame.pack(fill="both", expand=True, pady=5)

        self.txt_log = tk.Text(frame, height=12, wrap="word")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scrollbar.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def poll_log_queue(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get()
            if self.txt_log is not None:
                self.txt_log.insert("end", line + "\n")
                self.txt_log.see("end")
        self.after(200, self.poll_log_queue)

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
            self.tree_db.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    db.get("name", ""),
                    db.get("type", ""),
                    db.get("host", ""),
                    db.get("database", ""),
                    str(db.get("enabled", True)),
                ),
            )

    def on_add_db(self) -> None:
        dialog = DatabaseDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.db_entries.append(dialog.result)
            self.refresh_db_tree()

    def selected_db_index(self) -> int | None:
        if self.tree_db is None:
            return None
        selected = self.tree_db.selection()
        if not selected:
            return None
        return int(selected[0])

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

    def on_remove_db(self) -> None:
        idx = self.selected_db_index()
        if idx is None:
            messagebox.showinfo("No selection", "Please select a database entry first.")
            return
        self.db_entries.pop(idx)
        self.refresh_db_tree()

    def load_values_from_config(self, cfg: AppConfig) -> None:
        self.var_backup_root.set(cfg.backup_root)
        self.var_zip_output.set(cfg.zip_output)
        self.var_keep_sql.set(cfg.keep_sql_after_zip)

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
                "extra_args": list(db.extra_args),
            }
            for db in cfg.databases
        ]
        self.refresh_db_tree()

    def build_config_dict(self) -> dict:
        return {
            "backup_root": self.var_backup_root.get().strip() or "backups",
            "zip_output": bool(self.var_zip_output.get()),
            "keep_sql_after_zip": bool(self.var_keep_sql.get()),
            "telegram": {
                "enabled": bool(self.var_tg_enabled.get()),
                "bot_token": self.var_tg_token.get().strip(),
                "chat_id": self.var_tg_chat_id.get().strip(),
            },
            "schedule": {
                "enabled": bool(self.var_sched_enabled.get()),
                "mode": self.var_sched_mode.get().strip() or "daily",
                "time": self.var_sched_time.get().strip() or "02:00",
                "interval_minutes": int(self.var_sched_interval.get().strip() or "60"),
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
                    extra_args=list(db.get("extra_args", [])),
                )
            )

        return AppConfig(
            backup_root=self.var_backup_root.get().strip() or "backups",
            zip_output=bool(self.var_zip_output.get()),
            keep_sql_after_zip=bool(self.var_keep_sql.get()),
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

    def on_run_now(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            messagebox.showinfo("Busy", "Backup is already running.")
            return
        if self.btn_run_now is not None:
            self.btn_run_now.configure(state="disabled")
        self.run_thread = threading.Thread(target=self.run_once_worker, daemon=True)
        self.run_thread.start()

    def run_once_worker(self) -> None:
        try:
            cfg = self.build_app_config()
            selected_targets = parse_selected_targets(self.var_targets.get().strip())
            success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(
                cfg, selected_targets
            )
            msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
            logging.info(msg)
            send_telegram_message(cfg.telegram, msg)
        except Exception:
            logging.exception("Backup run failed")
        finally:
            self.after(0, self.enable_run_now_button)

    def enable_run_now_button(self) -> None:
        if self.btn_run_now is not None:
            self.btn_run_now.configure(state="normal")

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
        self.scheduler_thread = threading.Thread(
            target=self.auto_worker, args=(snapshot_cfg, self.var_targets.get().strip()), daemon=True
        )
        self.scheduler_thread.start()
        if self.btn_start_auto is not None:
            self.btn_start_auto.configure(state="disabled")
        if self.btn_stop_auto is not None:
            self.btn_stop_auto.configure(state="normal")
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
                    logging.info("Next run at %s", run_at.isoformat())
                    now = datetime.now(run_at.tzinfo)
                    wait_seconds = max((run_at - now).total_seconds(), 0)
                    if self.scheduler_stop_event.wait(wait_seconds):
                        break
                elif schedule.mode == "interval":
                    interval_seconds = max(schedule.interval_minutes, 1) * 60
                    if first_interval_cycle:
                        first_interval_cycle = False
                    else:
                        logging.info(
                            "Next run in %s minute(s)",
                            max(schedule.interval_minutes, 1),
                        )
                        if self.scheduler_stop_event.wait(interval_seconds):
                            break
                else:
                    raise ValueError("schedule.mode must be 'daily' or 'interval'")

                success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(
                    snapshot_cfg, selected_targets
                )
                msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
                logging.info(msg)
                send_telegram_message(snapshot_cfg.telegram, msg)
        except Exception:
            logging.exception("Auto backup crashed")
        finally:
            self.after(0, self.on_auto_stopped_ui)

    def on_auto_stopped_ui(self) -> None:
        if self.btn_start_auto is not None:
            self.btn_start_auto.configure(state="normal")
        if self.btn_stop_auto is not None:
            self.btn_stop_auto.configure(state="disabled")
        logging.info("Auto backup stopped.")


def main() -> int:
    app = BackupGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
