import csv
import io
import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import time
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


class WebGameTimeManager:
    """Session and limit management for the browser interface."""

    def __init__(self, tracker=None):
        self.tracker = tracker or GameUsageTracker()
        self.active_session_started_at = None
        self.active_session_date = date.today().isoformat()
        self.message = "制限内で使えます。"

    def _active_minutes(self):
        if self.active_session_started_at is None:
            return 0
        return max(0, int((time.time() - self.active_session_started_at) / 60))

    def stop_session(self, force_stop=False):
        if self.active_session_started_at is None:
            return False
        elapsed_minutes = self._active_minutes()
        if elapsed_minutes:
            self.tracker.log_session(self.active_session_date, elapsed_minutes)
        self.active_session_started_at = None
        self.message = (
            "上限に達したため、ゲームを自動終了しました。"
            if force_stop
            else "ゲームを終了しました。次のセッションも計画的に使いましょう。"
        )
        return True

    def enforce_limits(self):
        if self.active_session_started_at is None:
            return
        today = date.today().isoformat()
        active_minutes = self._active_minutes()
        if (
            self.tracker.get_daily_usage(today) + active_minutes >= self.tracker.get_daily_limit()
            or self.tracker.get_week_usage(today) + active_minutes >= self.tracker.get_weekly_limit()
        ):
            self.stop_session(force_stop=True)

    def start_session(self):
        self.enforce_limits()
        if self.active_session_started_at is not None:
            return False, "すでにゲーム中です。"

        today = date.today().isoformat()
        if self.tracker.is_daily_locked(today) or self.tracker.is_weekly_locked(today):
            self.message = "今日または今週の制限に達したため、ゲームを開始できません。"
            return False, self.message

        self.active_session_started_at = time.time()
        self.active_session_date = today
        self.message = "ゲーム中です。制限時間の管理を続けています。"
        return True, self.message

    def save_limits(self, daily_hours, weekly_hours):
        daily_hours = float(daily_hours)
        weekly_hours = float(weekly_hours)
        if not (
            math.isfinite(daily_hours)
            and math.isfinite(weekly_hours)
            and 0 < daily_hours <= 24
            and 0 < weekly_hours <= 24 * 7
        ):
            raise ValueError("1日の上限は0より大きく24時間以下、1週間の上限は0より大きく168時間以下で設定してください。")
        self.tracker.set_limits(int(daily_hours * 60), int(weekly_hours * 60))
        self.enforce_limits()
        self.message = "制限を保存しました。"

    def get_status(self):
        self.enforce_limits()
        today = date.today().isoformat()
        active_minutes = self._active_minutes()
        daily_limit = self.tracker.get_daily_limit()
        weekly_limit = self.tracker.get_weekly_limit()
        daily_usage = self.tracker.get_daily_usage(today) + active_minutes
        weekly_usage = self.tracker.get_week_usage(today) + active_minutes
        locked = daily_usage >= daily_limit or weekly_usage >= weekly_limit
        state = "ゲーム中" if self.active_session_started_at is not None else ("制限中" if locked else "待機中")
        message = self.message
        if locked and self.active_session_started_at is None:
            message = "本日または今週の上限に達したため、今はゲームを開始できません。"
        elif not locked and (
            daily_usage >= daily_limit * 0.8 or weekly_usage >= weekly_limit * 0.8
        ):
            message = "使用時間が上限の80%に達しました。残り時間を確認してください。"
        history = self.tracker.get_last_7_days_usage()
        if self.active_session_started_at is not None:
            for item in history:
                if item["date"] == today:
                    item["usage"] += active_minutes
                    break
        return {
            "state": state,
            "message": message,
            "active": self.active_session_started_at is not None,
            "can_start": not locked and self.active_session_started_at is None,
            "daily_usage": daily_usage,
            "weekly_usage": weekly_usage,
            "daily_limit": daily_limit,
            "weekly_limit": weekly_limit,
            "daily_remaining": max(0, daily_limit - daily_usage),
            "weekly_remaining": max(0, weekly_limit - weekly_usage),
            "history": history,
        }


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
    .state { margin: 0 0 6px; font-weight: 700; font-size: 1.5rem; }
    .state[data-state="ゲーム中"] { color: #15803d; }
    .state[data-state="制限中"] { color: #b91c1c; }
    .state[data-state="待機中"] { color: #1d4ed8; }
    .message { min-height: 3em; color: #475569; }
    .stats { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin: 18px 0; }
    .stats strong { font-variant-numeric: tabular-nums; }
    button { border: 0; border-radius: 9px; padding: 11px 16px; font: inherit; font-weight: 650; color: white; background: #2563eb; cursor: pointer; }
    button.stop { background: #475569; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .buttons { display: flex; gap: 10px; }
    label { display: block; margin: 0 0 14px; color: #475569; font-size: .92rem; }
    input { display: block; width: 100%; margin-top: 6px; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; }
    .chart { display: grid; grid-template-columns: repeat(7, 1fr); align-items: end; gap: 12px; height: 190px; padding-top: 10px; }
    .day { display: flex; flex-direction: column; justify-content: end; align-items: center; gap: 7px; height: 100%; color: #64748b; font-size: .8rem; }
    .bar-wrap { display: flex; align-items: end; width: 100%; height: 140px; }
    .bar { width: 100%; min-height: 2px; border-radius: 6px 6px 0 0; background: #60a5fa; }
    .notice { margin-top: 12px; color: #b91c1c; min-height: 1.4em; }
    @media (max-width: 480px) { body { padding: 20px 12px; } .card { padding: 17px; } .chart { gap: 6px; } }
  </style>
</head>
<body>
<main>
  <h1>ゲーム時間マネージャー</h1>
  <p class="subtitle">ゲームの使用時間を記録し、決めた上限を守りましょう。</p>
  <div class="grid">
    <section class="card">
      <h2>現在の状態</h2>
      <p class="state" id="state">読み込み中</p>
      <p class="message" id="message" aria-live="polite"></p>
      <div class="stats">
        <span>今日の使用時間</span><strong id="daily-usage">--</strong>
        <span>今日の残り時間</span><strong id="daily-remaining">--</strong>
        <span>今週の使用時間</span><strong id="weekly-usage">--</strong>
        <span>今週の残り時間</span><strong id="weekly-remaining">--</strong>
      </div>
      <div class="buttons">
        <button id="start" type="button" onclick="sessionAction('start')">ゲーム開始</button>
        <button class="stop" id="stop" type="button" onclick="sessionAction('stop')">ゲーム終了</button>
      </div>
      <p class="notice" id="notice" role="alert"></p>
    </section>
    <section class="card">
      <h2>制限設定</h2>
      <form id="limits-form">
        <label>1日の上限（時間）<input id="daily-limit" type="number" min="0.1" max="24" step="0.1" required></label>
        <label>1週間の上限（時間）<input id="weekly-limit" type="number" min="0.1" max="168" step="0.1" required></label>
        <button type="submit">制限を保存</button>
      </form>
    </section>
  </div>
  <section class="card" style="margin-top:16px">
    <h2>過去7日間の使用量（分）</h2>
    <div class="chart" id="chart" aria-label="過去7日間の使用量グラフ"></div>
  </section>
</main>
<script>
  const $ = (id) => document.getElementById(id);
  const formatMinutes = (minutes) => {
    const seconds = Math.max(0, Math.floor(minutes * 60));
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return [hours, mins, secs].map((n) => String(n).padStart(2, '0')).join(':');
  };
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
      value.textContent = String(item.usage);
      const wrap = document.createElement('div');
      wrap.className = 'bar-wrap';
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = `${Math.max(2, Math.min(100, item.usage / max * 100))}%`;
      wrap.append(bar);
      const label = document.createElement('span');
      label.textContent = item.date.slice(5);
      day.append(value, wrap, label);
      chart.append(day);
    }
  }
  async function refresh() {
    try {
      const data = await request('/api/status');
      $('state').textContent = data.state;
      $('state').dataset.state = data.state;
      $('message').textContent = data.message;
      $('daily-usage').textContent = `${formatMinutes(data.daily_usage)} / ${formatMinutes(data.daily_limit)}`;
      $('daily-remaining').textContent = formatMinutes(data.daily_remaining);
      $('weekly-usage').textContent = `${formatMinutes(data.weekly_usage)} / ${formatMinutes(data.weekly_limit)}`;
      $('weekly-remaining').textContent = formatMinutes(data.weekly_remaining);
      $('start').disabled = !data.can_start;
      $('stop').disabled = !data.active;
      if (document.activeElement !== $('daily-limit')) $('daily-limit').value = (data.daily_limit / 60).toFixed(1);
      if (document.activeElement !== $('weekly-limit')) $('weekly-limit').value = (data.weekly_limit / 60).toFixed(1);
      drawChart(data.history);
    } catch (error) {
      $('notice').textContent = error.message;
    }
  }
  async function sessionAction(action) {
    $('notice').textContent = '';
    try {
      const result = await request(`/api/${action}`, {});
      $('message').textContent = result.message;
      await refresh();
    } catch (error) {
      $('notice').textContent = error.message;
    }
  }
  $('limits-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('notice').textContent = '';
    try {
      const result = await request('/api/limits', {
        daily_hours: $('daily-limit').value,
        weekly_hours: $('weekly-limit').value
      });
      $('message').textContent = result.message;
      await refresh();
    } catch (error) {
      $('notice').textContent = error.message;
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
        self.manager.enforce_limits()


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
        if self.path not in {"/api/start", "/api/stop", "/api/limits"}:
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
            if self.path == "/api/start":
                success, message = manager.start_session()
                self._send_json({"success": success, "message": message}, 200 if success else 409)
            elif self.path == "/api/stop":
                if not manager.stop_session():
                    self._send_json({"error": "ゲームセッションは開始されていません。"}, 409)
                    return
                self._send_json({"success": True, "message": manager.message})
            else:
                manager.save_limits(payload.get("daily_hours"), payload.get("weekly_hours"))
                self._send_json({"success": True, "message": manager.message})
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
    print(f"ブラウザで開いてください: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを終了します。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
