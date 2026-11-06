import json
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta
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
                created_at TEXT NOT NULL
            )
            """
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

    def get_daily_limit(self):
        return self.get_limit("daily_minutes", 120)

    def get_weekly_limit(self):
        return self.get_limit("weekly_minutes", 420)

    def log_session(self, session_date, minutes):
        minutes = max(0, int(minutes))
        if minutes <= 0:
            return
        self.conn.execute(
            "INSERT INTO sessions(session_date, minutes, created_at) VALUES (?, ?, ?)",
            (session_date, minutes, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_daily_usage(self, session_date=None):
        target_date = (session_date or date.today().isoformat())
        row = self.conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) AS total FROM sessions WHERE session_date = ?",
            (target_date,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def get_week_usage(self, session_date=None):
        target_date = date.fromisoformat(session_date or date.today().isoformat())
        start_of_week = target_date - timedelta(days=target_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) AS total FROM sessions WHERE session_date BETWEEN ? AND ?",
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
    if tk is None:
        raise RuntimeError("Tkinter is not available in this environment.")

    root = tk.Tk()
    app = GameTimeManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
