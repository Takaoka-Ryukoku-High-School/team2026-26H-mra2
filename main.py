import argparse
import csv
import io
import json
import locale
import math
import os
import re
import sqlite3
import subprocess
import time
import webbrowser
from datetime import date, datetime, time as datetime_time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - GUI is optional for non-desktop tests
    tk = None
    ttk = None
    messagebox = None

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except Exception:  # pragma: no cover - handled gracefully if matplotlib is missing
    FigureCanvasTkAgg = None
    Figure = None


APP_DIR = Path.home() / ".game_time_manager"
DB_PATH = APP_DIR / "game_usage.db"
CONFIG_PATH = APP_DIR / "config.json"
NOTIFICATION_TONES = {"chime", "bell", "soft"}


def format_duration(total_seconds):
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class GameUsageTracker:
    """SQLite-backed daily/weekly game usage tracker."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path and db_path != ":memory:" else (":memory:" if db_path == ":memory:" else DB_PATH)
        if self.db_path != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path) if self.db_path != ":memory:" else ":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._seed_default_limits()

    def _init_db(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(sessions)")
        }
        if "seconds" not in columns:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN seconds INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.execute(
            "UPDATE sessions SET seconds = minutes * 60 WHERE seconds = 0 AND minutes > 0"
        )
        self.conn.commit()

    def _seed_default_limits(self):
        if self.get_limit("daily_minutes") is None:
            self.set_limits(120, 420)

    def _save_limit(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(int(value))),
        )
        self.conn.commit()

    def get_limit(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    def set_limits(self, daily_minutes, weekly_minutes):
        self._save_limit("daily_minutes", max(0, int(daily_minutes)))
        self._save_limit("weekly_minutes", max(0, int(weekly_minutes)))

    def get_weekday_limits(self):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'weekday_limits'"
        ).fetchone()
        if row is not None:
            try:
                limits = json.loads(row["value"])
                if (
                    isinstance(limits, list)
                    and len(limits) == 5
                    and all(isinstance(value, int) and 0 <= value <= 1440 for value in limits)
                ):
                    return limits
            except (TypeError, ValueError):
                pass
        return [self.get_daily_limit()] * 5

    def set_weekday_limits(self, limits):
        if (
            not isinstance(limits, list)
            or len(limits) != 5
            or any(not isinstance(value, int) or not 0 <= value <= 1440 for value in limits)
        ):
            raise ValueError("月曜日から金曜日までの上限を、0〜24時間の整数分で設定してください。")
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES('weekday_limits', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(limits),),
        )
        self.conn.commit()

    def get_process_names(self):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'process_names'"
        ).fetchone()
        if row is None:
            return []
        try:
            names = json.loads(row["value"])
        except (TypeError, ValueError) as error:
            raise ValueError("保存されたゲーム名の設定を読み込めません。") from error
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("保存されたゲーム名の設定が正しくありません。")
        return names

    def set_process_names(self, names):
        if not isinstance(names, list) or len(names) > 50:
            raise ValueError("ゲーム名は50個以内で設定してください。")
        normalized = []
        for name in names:
            if not isinstance(name, str):
                raise ValueError("ゲーム名は文字列で入力してください。")
            name = name.strip()
            if (
                not name
                or len(name) > 128
                or "/" in name
                or "\\" in name
                or any(ord(character) < 32 for character in name)
            ):
                raise ValueError("ゲーム名には実行ファイル名のみを入力してください（例: game.exe）。")
            folded_name = name.casefold()
            if folded_name not in {existing.casefold() for existing in normalized}:
                normalized.append(name)
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES('process_names', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(normalized, ensure_ascii=False),),
        )
        self.conn.commit()

    def get_notification_settings(self):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'notification_settings'"
        ).fetchone()
        if row is None:
            return {"tone": "chime", "volume": 50}
        try:
            settings = json.loads(row["value"])
        except (TypeError, ValueError) as error:
            raise ValueError("保存された通知音設定を読み込めません。") from error
        if (
            not isinstance(settings, dict)
            or not isinstance(settings.get("tone"), str)
            or settings["tone"] not in NOTIFICATION_TONES
            or type(settings.get("volume")) is not int
            or not 0 <= settings["volume"] <= 100
        ):
            raise ValueError("保存された通知音設定が正しくありません。")
        return settings

    def set_notification_settings(self, tone, volume):
        if not isinstance(tone, str) or tone not in NOTIFICATION_TONES:
            raise ValueError("通知音を選択してください。")
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("通知音の音量は0〜100で設定してください。")
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES('notification_settings', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"tone": tone, "volume": volume}),),
        )
        self.conn.commit()

    def get_daily_limit(self):
        return self.get_limit("daily_minutes", 120)

    def get_weekly_limit(self):
        return self.get_limit("weekly_minutes", 420)

    def log_session(self, session_date, minutes):
        minutes = max(0, int(minutes))
        if minutes <= 0:
            return
        self.log_session_seconds(session_date, minutes * 60)

    def log_session_seconds(self, session_date, seconds):
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return
        self.conn.execute(
            "INSERT INTO sessions(session_date, minutes, seconds, created_at) VALUES (?, ?, ?, ?)",
            (session_date, seconds // 60, seconds, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_daily_usage(self, session_date=None):
        return self.get_daily_usage_seconds(session_date) // 60

    def get_daily_usage_seconds(self, session_date=None):
        target_date = (session_date or date.today().isoformat())
        row = self.conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) AS total FROM sessions WHERE session_date = ?",
            (target_date,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_week_usage(self, session_date=None):
        return self.get_week_usage_seconds(session_date) // 60

    def get_week_usage_seconds(self, session_date=None):
        target_date = date.fromisoformat(session_date or date.today().isoformat())
        start_of_week = target_date - timedelta(days=target_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) AS total FROM sessions WHERE session_date BETWEEN ? AND ?",
            (start_of_week.isoformat(), end_of_week.isoformat()),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_remaining_daily(self, session_date=None):
        usage = self.get_daily_usage(session_date)
        return max(self.get_daily_limit() - usage, 0)

    def get_remaining_weekly(self, session_date=None):
        usage = self.get_week_usage(session_date)
        return max(self.get_weekly_limit() - usage, 0)

    def is_daily_locked(self, session_date=None):
        return self.get_daily_usage(session_date) >= self.get_daily_limit()

    def is_weekly_locked(self, session_date=None):
        return self.get_week_usage(session_date) >= self.get_weekly_limit()

    def get_last_7_days_usage(self):
        today = date.today()
        history = []
        for offset in range(6, -1, -1):
            d = today - timedelta(days=offset)
            history.append({"date": d.isoformat(), "usage": self.get_daily_usage(d.isoformat())})
        return history

    def get_current_week_usage_by_day(self, session_date=None):
        target_date = date.fromisoformat(session_date or date.today().isoformat())
        monday = target_date - timedelta(days=target_date.weekday())
        weekdays = ("月", "火", "水", "木", "金", "土", "日")
        history = []
        for offset, weekday in enumerate(weekdays):
            day = monday + timedelta(days=offset)
            history.append({
                "date": day.isoformat(),
                "weekday": weekday,
                "usage": self.get_daily_usage_seconds(day.isoformat()) / 60,
            })
        return history


class WebGameTimeManager:
    """Track configured game processes and weekday usage limits."""

    def __init__(self, tracker=None, process_provider=None):
        self.tracker = tracker or GameUsageTracker()
        self.process_provider = process_provider or self.get_running_processes
        self.process_active = False
        self.process_names = self.tracker.get_process_names()
        self.last_checked_at = None
        self.next_scan_at = 0
        self.scan_interval = 3
        self.monitor_error = None

    @staticmethod
    def get_running_processes():
        if os.name != "nt":
            raise RuntimeError("自動計測はWindows上でアプリを起動した場合に利用できます。")
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=True,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=10,
        )
        return {
            row[0].casefold()
            for row in csv.reader(io.StringIO(result.stdout))
            if row
        }

    def _record_interval(self, start_timestamp, end_timestamp):
        if end_timestamp <= start_timestamp:
            return
        cursor = datetime.fromtimestamp(start_timestamp)
        end = datetime.fromtimestamp(end_timestamp)
        while cursor.date() < end.date():
            midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime_time.min)
            seconds = int((midnight - cursor).total_seconds())
            self.tracker.log_session_seconds(cursor.date().isoformat(), seconds)
            cursor = midnight
        self.tracker.log_session_seconds(
            cursor.date().isoformat(), int((end - cursor).total_seconds())
        )

    def scan_processes(self, now=None, force=False):
        monotonic_now = time.monotonic()
        if not force and monotonic_now < self.next_scan_at:
            return
        self.next_scan_at = monotonic_now + self.scan_interval
        now = time.time() if now is None else now
        if not self.process_names:
            if self.process_active and self.last_checked_at is not None:
                self._record_interval(self.last_checked_at, now)
            self.process_active = False
            self.last_checked_at = None
            self.monitor_error = None
            return
        try:
            running = self.process_provider()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.monitor_error = str(error) or "ゲームのプロセスを確認できませんでした。"
            return

        matched_names = {name.casefold() for name in self.process_names}
        is_active = bool(matched_names.intersection(running))
        if self.process_active and self.last_checked_at is not None:
            self._record_interval(self.last_checked_at, now)
        self.process_active = is_active
        self.last_checked_at = now if is_active else None
        self.monitor_error = None

    def get_status(self):
        today = date.today().isoformat()
        weekday = date.today().weekday()
        daily_limit = self.tracker.get_weekday_limits()[weekday] if weekday < 5 else None
        pending_seconds = (
            max(0, int(time.time() - self.last_checked_at))
            if self.process_active and self.last_checked_at is not None
            else 0
        )
        daily_usage_seconds = self.tracker.get_daily_usage_seconds(today) + pending_seconds
        weekly_usage_seconds = self.tracker.get_week_usage_seconds(today) + pending_seconds
        daily_usage = daily_usage_seconds / 60
        weekly_usage = weekly_usage_seconds / 60
        daily_locked = daily_limit is not None and daily_usage_seconds >= daily_limit * 60
        if self.monitor_error:
            state = "計測できません"
            message = self.monitor_error
        elif not os.name == "nt":
            state = "計測できません"
            message = "自動計測はWindows上でアプリを起動した場合に利用できます。"
        elif not self.process_names:
            state = "ゲーム名未設定"
            message = "計測するゲームの実行ファイル名を設定してください。"
        elif self.process_active:
            state = "上限超過中" if daily_locked else "自動計測中"
            message = (
                "今日の設定時間を超えています。ゲームは自動終了しないため、終了してください。"
                if daily_locked
                else "登録したゲームの使用時間を自動計測しています。"
            )
        else:
            state = "上限到達" if daily_locked else "待機中"
            message = "今日の設定時間に達しました。" if daily_locked else "登録したゲームが起動すると自動計測します。"
        history = self.tracker.get_current_week_usage_by_day(today)
        if self.process_active:
            for item in history:
                if item["date"] == today:
                    item["usage"] += pending_seconds / 60
                    break
        return {
            "state": state,
            "message": message,
            "active": self.process_active,
            "daily_usage": daily_usage,
            "weekly_usage": weekly_usage,
            "daily_limit": daily_limit,
            "daily_remaining": (
                max(0, daily_limit * 60 - daily_usage_seconds) / 60
                if daily_limit is not None
                else None
            ),
            "weekday_limits": self.tracker.get_weekday_limits(),
            "process_names": self.process_names,
            "notification_settings": self.tracker.get_notification_settings(),
            "today": today,
            "monitor_supported": os.name == "nt",
            "history": history,
        }

    def save_settings(self, weekday_hours, process_names):
        if not isinstance(weekday_hours, list) or len(weekday_hours) != 5:
            raise ValueError("月曜日から金曜日までの上限を設定してください。")
        limits = []
        for hours in weekday_hours:
            value = float(hours)
            if not math.isfinite(value) or not 0 <= value <= 24:
                raise ValueError("曜日ごとの上限は0〜24時間で設定してください。")
            limits.append(int(value * 60))
        if not isinstance(process_names, list):
            raise ValueError("ゲーム名の一覧を入力してください。")
        self.tracker.set_process_names(process_names)
        self.tracker.set_weekday_limits(limits)
        self.process_names = self.tracker.get_process_names()
        self.monitor_error = None
        self.scan_processes(force=True)

    def save_notification_settings(self, tone, volume):
        self.tracker.set_notification_settings(tone, volume)


PAGE = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Game Time Manager</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; color: #172033; background: #f3f6fb; }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 32px 16px; }
    main { max-width: 900px; margin: auto; }
    h1 { margin: 0 0 6px; font-size: clamp(1.5rem, 4vw, 2rem); }
    .subtitle { margin: 0 0 24px; color: #64748b; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .card { padding: 22px; background: white; border: 1px solid #e2e8f0; border-radius: 16px; box-shadow: 0 4px 16px #0f172a0a; }
    h2 { margin: 0 0 16px; font-size: 1.1rem; }
    .home-actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-bottom: 16px; }
    button.nav-card { display: flex; align-items: center; gap: 14px; width: 100%; min-height: 100px; padding: 18px; color: #172033; text-align: left; background: white; border: 1px solid #e2e8f0; box-shadow: 0 4px 16px #0f172a0a; }
    .nav-icon { font-size: 1.8rem; }
    .nav-card strong, .nav-card small { display: block; }
    .nav-card small { margin-top: 5px; color: #64748b; font-weight: 400; }
    .view-heading { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
    .view-heading h2 { margin: 0; }
    .back-button, .secondary-button { color: #334155; background: #e2e8f0; }
    .view[hidden] { display: none; }
    .state { margin: 0 0 6px; font-weight: 700; font-size: 1.5rem; }
    .state[data-state="自動計測中"] { color: #15803d; }
    .state[data-state="上限超過中"], .state[data-state="上限到達"], .state[data-state="計測できません"] { color: #b91c1c; }
    .state[data-state="待機中"] { color: #1d4ed8; }
    .message { min-height: 3em; color: #475569; }
    .stats { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin: 18px 0; }
    .stats strong { font-variant-numeric: tabular-nums; }
    button { border: 0; border-radius: 9px; padding: 11px 16px; font: inherit; font-weight: 650; color: white; background: #2563eb; cursor: pointer; }
    label { display: block; margin: 0 0 14px; color: #475569; font-size: .92rem; }
    input { display: block; width: 100%; margin-top: 6px; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; }
    select { display: block; width: 100%; margin-top: 6px; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; background: white; }
    input[type="range"] { padding: 0; accent-color: #2563eb; }
    .volume-readout { display: flex; justify-content: space-between; color: #64748b; font-variant-numeric: tabular-nums; }
    .weekday-limits { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
    .chart { display: grid; grid-template-columns: repeat(7, 1fr); align-items: end; gap: 12px; height: 205px; padding-top: 10px; }
    .day { display: flex; flex-direction: column; justify-content: end; align-items: center; gap: 7px; height: 100%; min-width: 0; color: #64748b; font-size: .8rem; }
    .day-value { display: flex; align-items: end; justify-content: center; min-height: 2.2em; text-align: center; line-height: 1.1; overflow-wrap: anywhere; }
    .day-label { display: flex; flex-direction: column; align-items: center; line-height: 1.15; }
    .day-weekday { color: #334155; font-weight: 700; }
    .bar-wrap { display: flex; align-items: end; width: 100%; height: 125px; }
    .bar { width: 100%; min-height: 2px; border-radius: 6px 6px 0 0; background: #60a5fa; }
    .notice { margin-top: 12px; color: #b91c1c; min-height: 1.4em; }
    @media (max-width: 480px) { body { padding: 20px 12px; } .card { padding: 17px; } .chart { gap: 6px; } }
  </style>
</head>
<body>
<main>
  <h1>ゲーム時間マネージャー</h1>
  <p class="subtitle">登録したゲームを自動検出して使用時間を記録します。</p>
  <section class="view" id="home-view">
    <div class="home-actions">
      <button class="nav-card" type="button" data-view-target="sound-view">
        <span class="nav-icon" aria-hidden="true">🔔</span>
        <span><strong>通知音の設定</strong><small>音の種類や音量を調整</small></span>
      </button>
      <button class="nav-card" type="button" data-view-target="time-view">
        <span class="nav-icon" aria-hidden="true">⏱</span>
        <span><strong>時間設定</strong><small>曜日ごとの上限を設定</small></span>
      </button>
    </div>
    <section class="card">
      <h2>現在の状態</h2>
      <p class="state" id="state">読み込み中</p>
      <p class="message" id="message" aria-live="polite"></p>
      <div class="stats">
        <span>今日の使用時間</span><strong id="daily-usage">--</strong>
        <span>今日の上限</span><strong id="daily-limit-display">--</strong>
        <span>今日の残り時間</span><strong id="daily-remaining">--</strong>
        <span>今週の使用時間</span><strong id="weekly-usage">--</strong>
      </div>
      <p>計測対象: <strong id="tracked-processes">未設定</strong></p>
      <p class="notice" id="notice" role="alert"></p>
    </section>
    <section class="card" style="margin-top:16px">
      <h2>今週（月曜日〜日曜日）の使用時間</h2>
      <div class="chart" id="chart" aria-label="今週の月曜日から日曜日までの日別使用時間グラフ"></div>
    </section>
  </section>
  <section class="view" id="time-view" hidden>
    <div class="view-heading">
      <button class="back-button" type="button" data-view-target="home-view">← ホーム</button>
      <h2>時間設定</h2>
    </div>
    <section class="card">
      <h2>月曜日〜金曜日の一日の上限</h2>
      <form id="settings-form">
        <div class="weekday-limits">
          <label>月<input id="limit-0" type="number" min="0" max="24" step="0.1" required></label>
          <label>火<input id="limit-1" type="number" min="0" max="24" step="0.1" required></label>
          <label>水<input id="limit-2" type="number" min="0" max="24" step="0.1" required></label>
          <label>木<input id="limit-3" type="number" min="0" max="24" step="0.1" required></label>
          <label>金<input id="limit-4" type="number" min="0" max="24" step="0.1" required></label>
        </div>
        <label>ゲームの実行ファイル名（カンマ区切り）
          <input id="process-names" type="text" placeholder="game.exe, another-game.exe" autocomplete="off">
        </label>
        <p>ゲームを起動すると自動計測します。上限を超えてもゲームは自動終了しません。プロセス名はタスクマネージャーで確認できます。</p>
        <button type="submit">設定を保存</button>
        <p class="notice" id="time-notice" role="status"></p>
      </form>
    </section>
  </section>
  <section class="view" id="sound-view" hidden>
    <div class="view-heading">
      <button class="back-button" type="button" data-view-target="home-view">← ホーム</button>
      <h2>通知音の設定</h2>
    </div>
    <section class="card">
      <form id="sound-form">
        <label>通知音の種類
          <select id="notification-tone">
            <option value="chime">チャイム</option>
            <option value="bell">ベル</option>
            <option value="soft">やさしい音</option>
          </select>
        </label>
        <label for="notification-volume">音量</label>
        <input id="notification-volume" type="range" min="0" max="100" step="1">
        <div class="volume-readout"><span>ミュート</span><output id="volume-value">50%</output><span>最大</span></div>
        <p>上限の80%で通知し、上限到達時は音を繰り返します。試聴ボタンで確認できます。</p>
        <div class="home-actions">
          <button type="submit">設定を保存</button>
          <button class="secondary-button" id="preview-sound" type="button">試聴する</button>
        </div>
        <p class="notice" id="sound-notice" role="status"></p>
      </form>
    </section>
  </section>
</main>
<script>
  const $ = (id) => document.getElementById(id);
  let audioContext;
  let observedDay = null;
  let previousDailyUsage = null;
  let notificationSettingsLoaded = false;
  const formatMinutes = (minutes) => {
    const seconds = Math.max(0, Math.floor(minutes * 60));
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return [hours, mins, secs].map((n) => String(n).padStart(2, '0')).join(':');
  };
  const formatChartMinutes = (minutes) => {
    const totalMinutes = Math.floor(minutes);
    const hours = Math.floor(totalMinutes / 60);
    const mins = totalMinutes % 60;
    return hours ? `${hours}時間${mins ? `${mins}分` : ''}` : `${mins}分`;
  };
  function updateVolumeReadout() {
    const volume = Number($('notification-volume').value);
    $('volume-value').textContent = volume === 0 ? 'ミュート' : `${volume}%`;
  }
  async function playNotificationSound(settings, urgent = false) {
    if (settings.volume === 0) return;
    if (!window.AudioContext) throw new Error('このブラウザーでは通知音を再生できません。');
    audioContext = audioContext || new AudioContext();
    await audioContext.resume();
    const presets = {
      chime: { type: 'triangle', frequencies: [784, 988, 1175], duration: 0.18 },
      bell: { type: 'triangle', frequencies: [988, 1318], duration: 0.2 },
      soft: { type: 'sine', frequencies: [523, 659], duration: 0.24 }
    };
    const preset = presets[settings.tone];
    if (!preset) throw new Error('通知音の種類を確認してください。');
    let startAt = audioContext.currentTime;
    const frequencies = urgent
      ? [...preset.frequencies, ...preset.frequencies]
      : preset.frequencies;
    for (const frequency of frequencies) {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      const endAt = startAt + preset.duration;
      oscillator.type = preset.type;
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, startAt);
      gain.gain.linearRampToValueAtTime(settings.volume / 100 * 0.3, startAt + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, endAt);
      oscillator.connect(gain);
      gain.connect(audioContext.destination);
      oscillator.start(startAt);
      oscillator.stop(endAt);
      startAt = endAt + 0.06;
    }
  }
  function checkUsageAlerts(data) {
    if (observedDay !== data.today) {
      observedDay = data.today;
      previousDailyUsage = data.daily_usage;
      return;
    }
    const previous = previousDailyUsage;
    previousDailyUsage = data.daily_usage;
    if (previous === null || data.daily_limit === null || data.notification_settings.volume === 0) return;
    const limit = data.daily_limit;
    const reachedLimit = data.daily_usage >= limit &&
      (previous < limit || (limit === 0 && data.daily_usage > previous));
    const reachedWarning = limit > 0 && previous < limit * 0.8 && data.daily_usage >= limit * 0.8;
    if (!reachedLimit && !reachedWarning) return;
    playNotificationSound(data.notification_settings, reachedLimit).catch((error) => {
      $('notice').textContent = error.message;
    });
  }
  async function request(path, data) {
    const response = await fetch(path, {
      method: data ? 'POST' : 'GET',
      headers: data ? { 'Content-Type': 'application/json' } : {},
      body: data ? JSON.stringify(data) : undefined
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'リクエストに失敗しました。');
    return result;
  }
  function drawChart(history) {
    const chart = $('chart');
    chart.replaceChildren();
    const max = Math.max(60, ...history.map((item) => item.usage));
    for (const item of history) {
      const day = document.createElement('div');
      day.className = 'day';
      const value = document.createElement('span');
      value.className = 'day-value';
      value.textContent = formatChartMinutes(item.usage);
      const wrap = document.createElement('div');
      wrap.className = 'bar-wrap';
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = `${Math.max(2, Math.min(100, item.usage / max * 100))}%`;
      wrap.append(bar);
      const label = document.createElement('div');
      label.className = 'day-label';
      const weekday = document.createElement('span');
      weekday.className = 'day-weekday';
      weekday.textContent = item.weekday;
      const dateLabel = document.createElement('span');
      dateLabel.textContent = `${Number(item.date.slice(8))}日`;
      label.title = item.date;
      label.append(weekday, dateLabel);
      day.append(value, wrap, label);
      chart.append(day);
    }
  }
  document.querySelectorAll('[data-view-target]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.view').forEach((view) => {
        view.hidden = view.id !== button.dataset.viewTarget;
      });
    });
  });
  $('notification-volume').addEventListener('input', updateVolumeReadout);
  async function refresh() {
    try {
      const data = await request('/api/status');
      $('state').textContent = data.state;
      $('state').dataset.state = data.state;
      $('message').textContent = data.message;
      $('daily-usage').textContent = formatMinutes(data.daily_usage);
      $('daily-limit-display').textContent = data.daily_limit === null ? '設定なし' : formatMinutes(data.daily_limit);
      $('daily-remaining').textContent = data.daily_remaining === null ? '—' : formatMinutes(data.daily_remaining);
      $('weekly-usage').textContent = formatMinutes(data.weekly_usage);
      $('tracked-processes').textContent = data.process_names.join(', ') || '未設定';
      if (!notificationSettingsLoaded) {
        $('notification-tone').value = data.notification_settings.tone;
        $('notification-volume').value = data.notification_settings.volume;
        notificationSettingsLoaded = true;
      }
      updateVolumeReadout();
      for (let day = 0; day < 5; day++) {
        const input = $(`limit-${day}`);
        if (document.activeElement !== input) input.value = (data.weekday_limits[day] / 60).toFixed(1);
      }
      if (document.activeElement !== $('process-names')) $('process-names').value = data.process_names.join(', ');
      drawChart(data.history);
      checkUsageAlerts(data);
    } catch (error) {
      $('notice').textContent = error.message;
    }
  }
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('notice').textContent = '';
    try {
      const result = await request('/api/settings', {
        weekday_hours: Array.from({length: 5}, (_, day) => $(`limit-${day}`).value),
        process_names: $('process-names').value.split(/[,\\n;]/).map((name) => name.trim()).filter(Boolean)
      });
      $('time-notice').textContent = result.message;
      await refresh();
    } catch (error) {
      $('time-notice').textContent = error.message;
    }
  });
  $('sound-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('sound-notice').textContent = '';
    try {
      const result = await request('/api/notification-settings', {
        tone: $('notification-tone').value,
        volume: Number($('notification-volume').value)
      });
      $('sound-notice').textContent = result.message;
      await refresh();
    } catch (error) {
      $('sound-notice').textContent = error.message;
    }
  });
  $('preview-sound').addEventListener('click', async () => {
    $('sound-notice').textContent = '';
    try {
      await playNotificationSound({
        tone: $('notification-tone').value,
        volume: Number($('notification-volume').value)
      });
    } catch (error) {
      $('sound-notice').textContent = error.message;
    }
  });
  refresh();
  setInterval(refresh, 1000);
</script>
</body>
</html>"""


class GameTimeHTTPServer(HTTPServer):
    def __init__(self, server_address, manager):
        self.manager = manager
        super().__init__(server_address, GameTimeRequestHandler)

    def service_actions(self):
        self.manager.scan_processes()


class GameTimeRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._send_json(self.server.manager.get_status())
        else:
            self._send_json({"error": "ページが見つかりません。"}, 404)

    def do_POST(self):
        if self.path not in {"/api/settings", "/api/notification-settings"}:
            self._send_json({"error": "ページが見つかりません。"}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 10_000:
                raise ValueError("リクエストの内容を確認してください。")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("リクエストの内容を確認してください。")
            manager = self.server.manager
            if self.path == "/api/settings":
                manager.save_settings(
                    payload.get("weekday_hours"),
                    payload.get("process_names"),
                )
                message = "時間設定を保存しました。"
            else:
                manager.save_notification_settings(
                    payload.get("tone"),
                    payload.get("volume"),
                )
                message = "通知音の設定を保存しました。"
            self._send_json({"success": True, "message": message})
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            self._send_json({"error": str(error) or "入力値を確認してください。"}, 400)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


class GameTimeManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Game Time Manager")
        self.root.geometry("920x640")
        self.root.minsize(860, 560)

        self.tracker = GameUsageTracker()
        self.active_session_started_at = None
        self.active_session_date = date.today().isoformat()
        self.warning_sent = set()
        self.locked_notification_sent = False

        self._build_ui()
        self._sync_limits_from_tracker()
        self.refresh_all()
        self.root.after(1000, self._tick)

    def _build_ui(self):
        self.root.configure(bg="#f3f4f6")

        heading = ttk.Label(
            self.root,
            text="ゲーム依存症対策タイムマネージャー",
            font=("Helvetica", 22, "bold"),
            background="#f3f4f6",
        )
        heading.pack(anchor="w", padx=20, pady=(18, 8))

        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x", padx=16)

        limit_frame = ttk.LabelFrame(top, text="制限設定", padding=16)
        limit_frame.pack(side="left", fill="y", padx=(0, 16))

        ttk.Label(limit_frame, text="1日のゲーム時間（時間）").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 6))
        self.daily_limit_entry = ttk.Entry(limit_frame, width=12)
        self.daily_limit_entry.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(0, 6))

        ttk.Label(limit_frame, text="1週間のゲーム時間（時間）").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=(0, 6))
        self.weekly_limit_entry = ttk.Entry(limit_frame, width=12)
        self.weekly_limit_entry.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 6))

        ttk.Button(limit_frame, text="制限を保存", command=self._save_limits).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        status_frame = ttk.Frame(top)
        status_frame.pack(side="left", fill="both", expand=True)

        self.status_card = ttk.LabelFrame(status_frame, text="現在の状態", padding=18)
        self.status_card.pack(fill="x")

        self.status_label = ttk.Label(self.status_card, text="待機中", font=("Helvetica", 18, "bold"), foreground="#1d4ed8")
        self.status_label.pack(anchor="w")

        self.remaining_label = ttk.Label(self.status_card, text="残り: 00:00:00", font=("Helvetica", 14, "bold"))
        self.remaining_label.pack(anchor="w", pady=(8, 0))

        usage_row = ttk.Frame(self.status_card)
        usage_row.pack(fill="x", pady=(12, 0))
        ttk.Label(usage_row, text="今日の使用時間: ", font=("Helvetica", 11, "bold")).pack(side="left")
        self.today_usage_label = ttk.Label(usage_row, text="00:00:00", font=("Helvetica", 11))
        self.today_usage_label.pack(side="left")

        week_row = ttk.Frame(self.status_card)
        week_row.pack(fill="x", pady=(6, 0))
        ttk.Label(week_row, text="今週の使用時間: ", font=("Helvetica", 11, "bold")).pack(side="left")
        self.week_usage_label = ttk.Label(week_row, text="00:00:00", font=("Helvetica", 11))
        self.week_usage_label.pack(side="left")

        self.session_buttons = ttk.Frame(self.status_card)
        self.session_buttons.pack(fill="x", pady=(16, 0))
        self.start_button = ttk.Button(self.session_buttons, text="ゲーム開始", command=self.start_session)
        self.start_button.pack(side="left", padx=(0, 10))
        self.stop_button = ttk.Button(self.session_buttons, text="ゲーム終了", command=self.stop_session)
        self.stop_button.pack(side="left")

        self.message_var = tk.StringVar(value="制限内で使えます。")
        ttk.Label(self.status_card, textvariable=self.message_var, foreground="#374151", wraplength=520).pack(anchor="w", pady=(12, 0))

        graph_frame = ttk.LabelFrame(self.root, text="過去7日間の使用量", padding=12)
        graph_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.graph_container = ttk.Frame(graph_frame)
        self.graph_container.pack(fill="both", expand=True)

        self.chart_canvas = None

    def _sync_limits_from_tracker(self):
        self.daily_limit_entry.delete(0, tk.END)
        self.weekly_limit_entry.delete(0, tk.END)
        self.daily_limit_entry.insert(0, f"{self.tracker.get_daily_limit() / 60:.1f}")
        self.weekly_limit_entry.insert(0, f"{self.tracker.get_weekly_limit() / 60:.1f}")

    def _save_limits(self):
        try:
            daily_hours = float(self.daily_limit_entry.get())
            weekly_hours = float(self.weekly_limit_entry.get())
        except ValueError:
            messagebox.showwarning("入力エラー", "数値を正しく入力してください。")
            return

        if daily_hours <= 0 or weekly_hours <= 0:
            messagebox.showwarning("入力エラー", "上限は0より大きい値を設定してください。")
            return

        self.tracker.set_limits(int(daily_hours * 60), int(weekly_hours * 60))
        self._sync_limits_from_tracker()
        self.refresh_all()

    def _notify(self, title, message):
        if not message:
            return
        try:
            subprocess.run(["notify-send", title, message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            print(f"{title}: {message}")

    def _current_active_minutes(self):
        if self.active_session_started_at is None:
            return 0
        return max(0, int((time.time() - self.active_session_started_at) / 60))

    def _today_usage_minutes(self):
        return self.tracker.get_daily_usage(date.today().isoformat()) + self._current_active_minutes()

    def _week_usage_minutes(self):
        return self.tracker.get_week_usage(date.today().isoformat()) + self._current_active_minutes()

    def _check_limit_alerts(self):
        daily_limit = self.tracker.get_daily_limit()
        weekly_limit = self.tracker.get_weekly_limit()
        today_usage = self._today_usage_minutes()
        week_usage = self._week_usage_minutes()

        if self.active_session_started_at is not None:
            if today_usage >= daily_limit or week_usage >= weekly_limit:
                self.stop_session(force_stop=True)
                self._notify("ゲーム時間の上限に達しました", "本日のゲーム時間または今週の上限に達したため、ゲームを終了しました。")
                self.locked_notification_sent = True
                return

        if daily_limit > 0 and today_usage >= daily_limit * 0.8 and "daily_80" not in self.warning_sent:
            self.warning_sent.add("daily_80")
            self._notify("残り少なくなりました", "今日のゲーム時間が80%に達しました。残り時間を確認してください。")

        if weekly_limit > 0 and week_usage >= weekly_limit * 0.8 and "weekly_80" not in self.warning_sent:
            self.warning_sent.add("weekly_80")
            self._notify("今週の上限が近づいています", "今週のゲーム時間が80%を超えました。節制してください。")

    def start_session(self):
        today = date.today().isoformat()
        if self.active_session_started_at is not None:
            return

        if self.tracker.is_daily_locked(today) or self.tracker.is_weekly_locked(today):
            self.message_var.set("今日の制限に達したため、これ以上は開始できません。")
            self._notify("制限中", "今日のゲーム時間上限に達したため、新しいゲーム開始はできません。")
            return

        self.active_session_started_at = time.time()
        self.active_session_date = today
        self.message_var.set("ゲーム中です。制限時間の管理を続けています。")
        self.status_label.configure(text="ゲーム中", foreground="#15803d")
        self.refresh_all()

    def stop_session(self, force_stop=False):
        if self.active_session_started_at is None:
            return

        elapsed_minutes = self._current_active_minutes()
        if elapsed_minutes > 0:
            self.tracker.log_session(self.active_session_date, elapsed_minutes)

        self.active_session_started_at = None
        self.warning_sent.clear()
        self.locked_notification_sent = False

        if force_stop:
            self.message_var.set("上限に達したため、ゲームを自動終了しました。")
        else:
            self.message_var.set("ゲームを終了しました。次のセッションも計画的に使いましょう。")

        self.refresh_all()

    def refresh_all(self):
        today = date.today().isoformat()
        today_usage = self._today_usage_minutes()
        week_usage = self._week_usage_minutes()

        daily_limit = self.tracker.get_daily_limit()
        weekly_limit = self.tracker.get_weekly_limit()

        remaining = max(daily_limit - today_usage, 0)
        remaining_week = max(weekly_limit - week_usage, 0)

        self.today_usage_label.configure(text=format_duration(today_usage * 60))
        self.week_usage_label.configure(text=format_duration(week_usage * 60))
        self.remaining_label.configure(text=f"残り: {format_duration(remaining * 60)}")

        if self.active_session_started_at is not None:
            self.status_label.configure(text="ゲーム中", foreground="#15803d")
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])
        else:
            self.status_label.configure(text="待機中", foreground="#1d4ed8")
            self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])

        if today_usage >= daily_limit or week_usage >= weekly_limit:
            self.status_label.configure(text="制限終了", foreground="#b91c1c")
            self.message_var.set("本日のまたは今週の制限時間を超えたため、今はゲームできません。")
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])

        self._render_chart()

    def _render_chart(self):
        data = self.tracker.get_last_7_days_usage()
        if Figure is None or FigureCanvasTkAgg is None:
            ttk.Label(self.graph_container, text="グラフ表示のために matplotlib が必要です。").pack(anchor="center")
            return

        for child in self.graph_container.winfo_children():
            child.destroy()

        fig = Figure(figsize=(8, 3.6), dpi=100)
        ax = fig.add_subplot(111)
        dates = [item["date"][-2:] for item in data]
        values = [item["usage"] for item in data]
        ax.bar(dates, values, color="#60a5fa", alpha=0.8)
        ax.set_title("7日間のゲーム時間（分）")
        ax.set_ylabel("分")
        ax.set_ylim(0, max(120, max(values, default=0) + 30))
        ax.grid(True, linestyle="--", alpha=0.35)

        canvas = FigureCanvasTkAgg(fig, master=self.graph_container)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

    def _tick(self):
        self._check_limit_alerts()
        self.refresh_all()
        self.root.after(1000, self._tick)


def main():
    parser = argparse.ArgumentParser(description="ゲーム時間マネージャー")
    parser.add_argument("--gui", action="store_true", help="従来のデスクトップ画面で起動")
    parser.add_argument("--host", default="127.0.0.1", help="Webサーバーの接続先 (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Webサーバーのポート (default: 8000)")
    args = parser.parse_args()

    if args.gui:
        if tk is None:
            raise RuntimeError("Tkinter is not available in this environment.")
        root = tk.Tk()
        app = GameTimeManagerApp(root)
        root.mainloop()
        return

    manager = WebGameTimeManager()
    server = GameTimeHTTPServer((args.host, args.port), manager)
    browser_host = args.host
    if browser_host in {"0.0.0.0", "::"}:
        browser_host = "127.0.0.1"
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    url = f"http://{browser_host}:{server.server_port}/"
    print(f"アプリのURL: {url}")
    if not webbrowser.open(url):
        print("ブラウザを自動で開けませんでした。上記URLを手動で開いてください。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを終了します。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
