#!/usr/bin/env python3
"""
Database backup utility for MySQL/PostgreSQL with:
- manual run or scheduled run
- optional ZIP compression
- Telegram notifications
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib import parse, request
from zoneinfo import ZoneInfo
import zipfile


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class ScheduleConfig:
    enabled: bool = False
    mode: str = "daily"  # daily | interval
    time: str = "02:00"  # HH:MM for daily
    interval_minutes: int = 60
    timezone: str = "UTC"


@dataclass
class DatabaseConfig:
    name: str
    db_type: str  # mysql | postgresql
    host: str
    port: int
    username: str
    password: str
    database: str
    enabled: bool = True
    skip_log_tables: bool | None = None
    exclude_tables: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    backup_root: str = "backups"
    zip_output: bool = True
    keep_sql_after_zip: bool = False
    skip_log_tables: bool = False
    log_table_keywords: list[str] = field(
        default_factory=lambda: ["log", "logs", "audit", "history", "event_log"]
    )
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    databases: list[DatabaseConfig] = field(default_factory=list)


def load_config(config_path: Path) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    telegram = TelegramConfig(**raw.get("telegram", {}))
    schedule = ScheduleConfig(**raw.get("schedule", {}))

    databases: list[DatabaseConfig] = []
    for db in raw.get("databases", []):
        mapped = dict(db)
        if "type" in mapped:
            mapped["db_type"] = mapped.pop("type")
        databases.append(DatabaseConfig(**mapped))

    return AppConfig(
        backup_root=raw.get("backup_root", "backups"),
        zip_output=raw.get("zip_output", True),
        keep_sql_after_zip=raw.get("keep_sql_after_zip", False),
        skip_log_tables=raw.get("skip_log_tables", False),
        log_table_keywords=raw.get(
            "log_table_keywords",
            ["log", "logs", "audit", "history", "event_log"],
        ),
        telegram=telegram,
        schedule=schedule,
        databases=databases,
    )


def send_telegram_message(cfg: TelegramConfig, message: str) -> None:
    if not cfg.enabled:
        return
    if not cfg.bot_token or not cfg.chat_id:
        logging.warning("Telegram enabled but bot_token/chat_id missing")
        return

    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    data = parse.urlencode({"chat_id": cfg.chat_id, "text": message}).encode("utf-8")
    req = request.Request(url=url, data=data, method="POST")
    try:
        with request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                logging.error("Telegram notification failed with status=%s", resp.status)
    except Exception as exc:
        logging.error("Telegram notification failed: %s", exc)


def run_subprocess(command: list[str], output_file: Path, env: dict[str, str] | None = None) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("wb") as out:
        proc = subprocess.run(
            command,
            stdout=out,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
    if proc.returncode != 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{stderr_msg}")


def run_subprocess_capture(
    command: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def safe_identifier(raw: str) -> str:
    return raw.replace("`", "``").replace("'", "''")


def should_skip_log_tables(cfg: AppConfig, db: DatabaseConfig) -> bool:
    if db.skip_log_tables is not None:
        return db.skip_log_tables
    return cfg.skip_log_tables


def discover_mysql_log_tables(db: DatabaseConfig, keywords: list[str]) -> list[str]:
    cleaned = [kw.strip().lower() for kw in keywords if kw and kw.strip()]
    if not cleaned:
        return []

    db_name = safe_identifier(db.database)
    conditions = " OR ".join(f"LOWER(table_name) LIKE '%{safe_identifier(kw)}%'" for kw in cleaned)
    query = (
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{db_name}' AND ({conditions});"
    )
    command = [
        "mysql",
        "-N",
        "-h",
        db.host,
        "-P",
        str(db.port),
        "-u",
        db.username,
        f"--password={db.password}",
        "-e",
        query,
    ]

    proc = run_subprocess_capture(command)
    if proc.returncode != 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace")
        logging.warning(
            "Cannot discover MySQL log tables for %s. Skip auto-exclude. reason=%s",
            db.name,
            stderr_msg.strip(),
        )
        return []

    raw_tables = proc.stdout.decode("utf-8", errors="replace").splitlines()
    return [tbl.strip() for tbl in raw_tables if tbl.strip()]


def build_mysql_command(db: DatabaseConfig, ignored_tables: list[str]) -> list[str]:
    command = [
        "mysqldump",
        "-h",
        db.host,
        "-P",
        str(db.port),
        "-u",
        db.username,
        f"--password={db.password}",
        "--single-transaction",
        "--databases",
        db.database,
    ]
    for table in ignored_tables:
        if "." in table:
            scoped = table
        else:
            scoped = f"{db.database}.{table}"
        command.append(f"--ignore-table={scoped}")
    command.extend(db.extra_args)
    return command


def build_postgresql_command(db: DatabaseConfig, exclude_patterns: list[str]) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env["PGPASSWORD"] = db.password
    command = [
        "pg_dump",
        "-h",
        db.host,
        "-p",
        str(db.port),
        "-U",
        db.username,
        "-d",
        db.database,
    ]
    for pattern in exclude_patterns:
        command.extend(["--exclude-table", pattern])
    command.extend(db.extra_args)
    return command, env


def zip_file(input_file: Path) -> Path:
    zip_path = input_file.with_suffix(input_file.suffix + ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(input_file, arcname=input_file.name)
    return zip_path


def format_duration(seconds: float) -> str:
    total_seconds = max(int(round(seconds)), 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def backup_database(db: DatabaseConfig, cfg: AppConfig, backup_dir: Path) -> tuple[Path, float]:
    started_at = time.perf_counter()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = backup_dir / f"{db.name}_{timestamp}.sql"
    skip_log_tables = should_skip_log_tables(cfg, db)
    keywords = [kw.strip() for kw in cfg.log_table_keywords if kw and kw.strip()]
    excluded_tables = [tbl.strip() for tbl in db.exclude_tables if tbl and tbl.strip()]

    logging.info("Backing up %s (%s)...", db.name, db.db_type)
    if db.db_type == "mysql":
        auto_ignored: list[str] = []
        if skip_log_tables:
            auto_ignored = discover_mysql_log_tables(db, keywords)
        ignored_tables = sorted({*excluded_tables, *auto_ignored})
        if ignored_tables:
            logging.info("MySQL excluded tables for %s: %s", db.name, ", ".join(ignored_tables))
        command = build_mysql_command(db, ignored_tables)
        run_subprocess(command, out_file)
    elif db.db_type == "postgresql":
        excluded_patterns = list(excluded_tables)
        if skip_log_tables:
            excluded_patterns.extend([f"*{kw}*" for kw in keywords])
        if excluded_patterns:
            logging.info(
                "PostgreSQL excluded tables/patterns for %s: %s",
                db.name,
                ", ".join(excluded_patterns),
            )
        command, env = build_postgresql_command(db, excluded_patterns)
        run_subprocess(command, out_file, env=env)
    else:
        raise ValueError(f"Unsupported db type for {db.name}: {db.db_type}")

    if cfg.zip_output:
        zip_path = zip_file(out_file)
        if not cfg.keep_sql_after_zip:
            out_file.unlink(missing_ok=True)
        logging.info("Backup zipped: %s", zip_path)
        elapsed = time.perf_counter() - started_at
        return zip_path, elapsed

    logging.info("Backup created: %s", out_file)
    elapsed = time.perf_counter() - started_at
    return out_file, elapsed


def parse_selected_targets(targets: str | None) -> set[str] | None:
    if not targets:
        return None
    parsed = {name.strip() for name in targets.split(",") if name.strip()}
    return parsed or None


def select_databases(databases: list[DatabaseConfig], selected: set[str] | None) -> list[DatabaseConfig]:
    enabled = [db for db in databases if db.enabled]
    if selected is None:
        return enabled
    return [db for db in enabled if db.name in selected]


def execute_backup_cycle(
    cfg: AppConfig,
    selected_targets: set[str] | None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[bool, list[Path], list[str], float, dict[str, float]]:
    def report_progress(percent: float, message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(percent, message)
        except Exception:
            logging.exception("Progress callback failed")

    cycle_started = time.perf_counter()
    backup_root = Path(cfg.backup_root)
    day_dir = backup_root / datetime.now().strftime("%Y-%m-%d")
    targets = select_databases(cfg.databases, selected_targets)

    if not targets:
        report_progress(0.0, "No target databases selected or enabled.")
        return False, [], ["No target databases selected or enabled."], 0.0, {}

    total_targets = len(targets)
    report_progress(0.0, f"Backup started for {total_targets} database(s)")

    success_files: list[Path] = []
    errors: list[str] = []
    durations_by_db: dict[str, float] = {}

    for idx, db in enumerate(targets, start=1):
        started_percent = ((idx - 1) / total_targets) * 100
        report_progress(started_percent, f"Starting backup {idx}/{total_targets}: {db.name}")
        try:
            artifact, elapsed = backup_database(
                db=db,
                cfg=cfg,
                backup_dir=day_dir,
            )
            success_files.append(artifact)
            durations_by_db[db.name] = elapsed
            logging.info("Backup %s completed in %s", db.name, format_duration(elapsed))
            finished_percent = (idx / total_targets) * 100
            report_progress(finished_percent, f"Completed {db.name} ({finished_percent:.0f}%)")
        except Exception as exc:
            msg = f"{db.name}: {exc}"
            logging.exception("Backup failed for %s", db.name)
            errors.append(msg)
            finished_percent = (idx / total_targets) * 100
            report_progress(finished_percent, f"Failed {db.name} ({finished_percent:.0f}%)")

    overall_success = len(errors) == 0
    total_elapsed = time.perf_counter() - cycle_started
    report_progress(100.0, f"Backup cycle finished in {format_duration(total_elapsed)}")
    return overall_success, success_files, errors, total_elapsed, durations_by_db


def build_result_message(
    success: bool,
    files: list[Path],
    errors: list[str],
    total_elapsed: float,
    durations_by_db: dict[str, float],
) -> str:
    lines: list[str] = []
    if success:
        lines.append("✅ Backup success")
    else:
        lines.append("❌ Backup finished with errors")

    lines.append(f"Total time: {format_duration(total_elapsed)}")

    if durations_by_db:
        lines.append("Per database:")
        for name, seconds in durations_by_db.items():
            lines.append(f"- {name}: {format_duration(seconds)}")

    if files:
        lines.append("Artifacts:")
        for path in files:
            lines.append(f"- {path}")

    if errors:
        lines.append("Errors:")
        for err in errors:
            lines.append(f"- {err}")

    return "\n".join(lines)


def next_daily_run(schedule_time: str, timezone_name: str) -> datetime:
    now = datetime.now(ZoneInfo(timezone_name))
    hour, minute = schedule_time.split(":")
    candidate = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def sleep_until(target_dt: datetime) -> None:
    while True:
        now = datetime.now(target_dt.tzinfo)
        diff = (target_dt - now).total_seconds()
        if diff <= 0:
            return
        time.sleep(min(diff, 60))


def run_scheduler(cfg: AppConfig, selected_targets: set[str] | None) -> None:
    schedule = cfg.schedule
    if schedule.mode not in {"daily", "interval"}:
        raise ValueError("schedule.mode must be 'daily' or 'interval'")

    logging.info("Scheduler started (mode=%s)", schedule.mode)

    first_interval_cycle = True
    while True:
        if schedule.mode == "daily":
            run_at = next_daily_run(schedule.time, schedule.timezone)
            logging.info("Next run at %s", run_at.isoformat())
            sleep_until(run_at)
        else:
            interval = max(schedule.interval_minutes, 1)
            if first_interval_cycle:
                first_interval_cycle = False
            else:
                logging.info("Sleeping %s minute(s) before next run", interval)
                time.sleep(interval * 60)

        success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(cfg, selected_targets)
        msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
        send_telegram_message(cfg.telegram, msg)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Database backup tool (MySQL/PostgreSQL)")
    parser.add_argument(
        "--config",
        default="backup_config.json",
        help="Path to JSON config file (default: backup_config.json)",
    )
    parser.add_argument(
        "--targets",
        default=None,
        help="Comma-separated target names to backup (match 'name' in config)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run in scheduler mode using schedule settings in config",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = build_arg_parser().parse_args()
    cfg = load_config(Path(args.config))
    selected_targets = parse_selected_targets(args.targets)

    if args.schedule:
        if not cfg.schedule.enabled:
            logging.warning("Schedule config is disabled, but --schedule was requested.")
        run_scheduler(cfg, selected_targets)
        return 0

    success, files, errors, total_elapsed, durations_by_db = execute_backup_cycle(cfg, selected_targets)
    msg = build_result_message(success, files, errors, total_elapsed, durations_by_db)
    if success:
        logging.info("Backup completed in %s", format_duration(total_elapsed))
        send_telegram_message(cfg.telegram, msg)
        return 0

    logging.error(msg)
    send_telegram_message(cfg.telegram, msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
