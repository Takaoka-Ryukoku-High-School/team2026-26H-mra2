import asyncio
import json
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import flet as ft


DATA_FILE = Path.home() / ".daily_note_tasks.json"
GAME_TIMER_FILE = Path.home() / ".game_time_limit.json"
GREEN = "#2E6654"
GREEN_LIGHT = "#E2EDE5"
PAPER = "#F6F5EF"
INK = "#202C2A"
MUTED = "#77807B"
CORAL = "#CF6048"


def load_tasks():
    try:
        tasks = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(tasks, list):
            return [
                task
                for task in tasks
                if isinstance(task, dict)
                and isinstance(task.get("id"), str)
                and isinstance(task.get("text"), str)
                and isinstance(task.get("done"), bool)
            ]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_tasks(tasks):
    DATA_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def load_timer_state():
    try:
        state = json.loads(GAME_TIMER_FILE.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return {
                "date": str(state.get("date", "")),
                "limit_seconds": max(60, int(state.get("limit_seconds", 3600))),
                "elapsed_seconds": max(0, int(state.get("elapsed_seconds", 0))),
                "started_at": max(0.0, float(state.get("started_at", 0))),
            }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"date": "", "limit_seconds": 3600, "elapsed_seconds": 0, "started_at": 0}


def save_timer_state(state):
    try:
        GAME_TIMER_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def main(page: ft.Page):
    page.title = "今日の段取り | ToDo"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = PAPER
    page.padding = 24
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.scroll = ft.ScrollMode.AUTO

    tasks = load_tasks()
    current_filter = "all"

    today = date.today()
    timer_state = load_timer_state()
    if timer_state["date"] != today.isoformat():
        timer_state = {
            "date": today.isoformat(),
            "limit_seconds": timer_state["limit_seconds"],
            "elapsed_seconds": 0,
            "started_at": 0,
        }
        save_timer_state(timer_state)

    timer_running = timer_state["started_at"] > 0
    limit_hours = ft.TextField(
        label="1日の上限（時間）",
        value=str(timer_state["limit_seconds"] / 3600),
        width=180,
        input_filter=ft.NumbersOnlyInputFilter(),
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    remaining_text = ft.Text(size=44, weight=ft.FontWeight.BOLD, color=INK)
    timer_status = ft.Text(size=12, color=MUTED)
    timer_button = ft.Button(
        "ゲーム開始",
        width=140,
        height=44,
        bgcolor=GREEN,
        color="white",
        on_click=lambda event: toggle_timer(event),
    )

    def current_elapsed():
        elapsed = timer_state["elapsed_seconds"]
        if timer_state["started_at"]:
            elapsed += max(0, time.time() - timer_state["started_at"])
        return min(elapsed, timer_state["limit_seconds"])

    def refresh_timer():
        nonlocal timer_running
        if timer_state["date"] != date.today().isoformat():
            timer_state.update(
                date=date.today().isoformat(),
                limit_seconds=timer_state["limit_seconds"],
                elapsed_seconds=0,
                started_at=0,
            )
            save_timer_state(timer_state)

        elapsed = current_elapsed()
        remaining = timer_state["limit_seconds"] - elapsed
        timer_running = timer_state["started_at"] > 0
        remaining_text.value = format_duration(remaining)
        timer_status.value = "残り時間" if timer_running else "停止中"
        timer_button.text = "停止" if timer_running else "ゲーム開始"
        timer_button.bgcolor = CORAL if timer_running else GREEN
        timer_button.color = "white"

        if remaining <= 0:
            timer_state["elapsed_seconds"] = timer_state["limit_seconds"]
            timer_state["started_at"] = 0
            timer_running = False
            timer_status.value = "今日の上限に到達"
            timer_button.text = "リセット"
            timer_button.bgcolor = GREEN
            save_timer_state(timer_state)
        page.update()

    def apply_limit(event):
        try:
            hours = float(limit_hours.value)
            if not 0.25 <= hours <= 24:
                raise ValueError
        except (TypeError, ValueError):
            timer_status.value = "0.25〜24時間で入力してください"
            page.update()
            return

        elapsed = current_elapsed()
        timer_state["limit_seconds"] = int(hours * 3600)
        timer_state["elapsed_seconds"] = min(int(elapsed), timer_state["limit_seconds"])
        timer_state["started_at"] = time.time() if timer_running else 0
        save_timer_state(timer_state)
        refresh_timer()

    def toggle_timer(event):
        if current_elapsed() >= timer_state["limit_seconds"]:
            timer_state["elapsed_seconds"] = 0
            timer_state["started_at"] = 0
            save_timer_state(timer_state)
            refresh_timer()
            return

        if timer_state["started_at"]:
            timer_state["elapsed_seconds"] = int(current_elapsed())
            timer_state["started_at"] = 0
        else:
            timer_state["started_at"] = time.time()
        save_timer_state(timer_state)
        refresh_timer()

    async def update_timer():
        while True:
            await asyncio.sleep(1)
            refresh_timer()

    task_input = ft.TextField(
        hint_text="次にやることは？",
        expand=True,
        autofocus=True,
        border_radius=6,
        border_color="#D4DED5",
        focused_border_color=GREEN,
        bgcolor="#FFFEFA",
        text_size=15,
        on_submit=lambda event: add_task(event),
    )
    task_list = ft.Column(spacing=0)
    task_count = ft.Text(color=MUTED, size=12)
    progress_label = ft.Text(color=MUTED, size=11)
    clear_done_button = ft.TextButton(
        "完了した項目を削除",
        icon=ft.Icons.DELETE_OUTLINE,
        style=ft.ButtonStyle(color=CORAL),
        on_click=lambda event: clear_done(event),
    )
    filter_buttons = {}

    def persist():
        try:
            save_tasks(tasks)
            progress_label.value = f"{sum(task['done'] for task in tasks)} / {len(tasks)} 件 完了"
        except OSError:
            progress_label.value = "保存できませんでした。書き込み権限をご確認ください。"

    def set_filter(value):
        nonlocal current_filter
        current_filter = value
        render()

    def add_task(event):
        text = task_input.value.strip()
        if not text:
            return
        tasks.insert(0, {"id": str(uuid4()), "text": text, "done": False})
        task_input.value = ""
        persist()
        set_filter("all")

    def toggle_task(task_id):
        for task in tasks:
            if task["id"] == task_id:
                task["done"] = not task["done"]
                break
        persist()
        render()

    def delete_task(task_id):
        tasks[:] = [task for task in tasks if task["id"] != task_id]
        persist()
        render()

    def clear_done(event):
        tasks[:] = [task for task in tasks if not task["done"]]
        persist()
        render()

    def render():
        visible_tasks = [
            task
            for task in tasks
            if current_filter == "all"
            or (current_filter == "active" and not task["done"])
            or (current_filter == "done" and task["done"])
        ]
        remaining = sum(not task["done"] for task in tasks)
        completed = len(tasks) - remaining
        task_count.value = f"未完了 {remaining} 件"
        progress_label.value = (
            f"{completed} / {len(tasks)} 件 完了" if tasks else "タスクはまだありません"
        )
        clear_done_button.visible = completed > 0

        for key, button in filter_buttons.items():
            selected = key == current_filter
            button.style = ft.ButtonStyle(
                color=GREEN if selected else MUTED,
                bgcolor=GREEN_LIGHT if selected else "transparent",
                shape=ft.RoundedRectangleBorder(radius=4),
            )

        rows = []
        for task in visible_tasks:
            checkbox = ft.Checkbox(
                value=task["done"],
                label=task["text"],
                expand=True,
                active_color=GREEN,
                label_style=ft.TextStyle(
                    color="#929992" if task["done"] else INK,
                    decoration=ft.TextDecoration.LINE_THROUGH if task["done"] else None,
                    size=14,
                ),
                on_change=lambda event, task_id=task["id"]: toggle_task(task_id),
            )
            delete_button = ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                icon_color=MUTED,
                tooltip=f"「{task['text']}」を削除",
                on_click=lambda event, task_id=task["id"]: delete_task(task_id),
            )
            rows.extend(
                [
                    ft.Row([checkbox, delete_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Divider(height=1, thickness=1, color="#DEDFD5"),
                ]
            )

        if not visible_tasks:
            empty_text = (
                "未完了のタスクはありません。いい調子！"
                if current_filter == "active" and tasks
                else "完了したタスクはまだありません。"
                if current_filter == "done" and tasks
                else "まずは、やることをひとつ書いてみましょう。"
            )
            rows = [
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Text("○", size=24, color="#9A7629", text_align=ft.TextAlign.CENTER),
                            ft.Text(empty_text, size=13, color=MUTED, text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=10,
                    ),
                    padding=32,
                    alignment=ft.Alignment.CENTER,
                )
            ]

        task_list.controls = rows
        page.update()

    for key, label in (("all", "すべて"), ("active", "未完了"), ("done", "完了")):
        filter_buttons[key] = ft.TextButton(
            label,
            on_click=lambda event, value=key: set_filter(value),
        )

    today_text = ft.Text(
        today.strftime("%Y年%m月%d日"),
        size=12,
        color=MUTED,
    )
    composer = ft.Row(
        [
            task_input,
            ft.Button(
                "追加  ＋",
                on_click=add_task,
                bgcolor=GREEN,
                color="white",
                height=48,
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=5)),
            ),
        ],
        spacing=10,
    )

    content = ft.Column(
        [
            ft.Row(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Text("日", color="white", size=15, weight=ft.FontWeight.BOLD),
                                width=28,
                                height=28,
                                bgcolor=GREEN,
                                border_radius=14,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Text("DAILY NOTE", size=12, color=GREEN, weight=ft.FontWeight.BOLD),
                        ],
                        spacing=10,
                    ),
                    today_text,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Column(
                [
                    ft.Text("今日の段取り", size=38, color=INK, weight=ft.FontWeight.W_600),
                    ft.Text("ひとつずつ、進めていこう。", size=13, color=MUTED),
                ],
                spacing=4,
            ),
            ft.Container(
                content=ft.Column(
                    [
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text("ゲーム時間", size=22, weight=ft.FontWeight.BOLD, color=INK),
                                            timer_status,
                                            remaining_text,
                                        ],
                                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                        expand=True,
                                    ),
                                    ft.Column(
                                        [
                                            limit_hours,
                                            ft.Button("上限を保存", on_click=apply_limit, height=40),
                                            timer_button,
                                        ],
                                        horizontal_alignment=ft.CrossAxisAlignment.END,
                                        spacing=8,
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=18,
                            bgcolor=GREEN_LIGHT,
                        ),
                        ft.Container(content=composer, padding=18),
                        ft.Container(
                            content=ft.Row(
                                [ft.Row(list(filter_buttons.values()), spacing=0), task_count],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            ),
                            padding=ft.Padding(left=18, right=18, top=8, bottom=8),
                            border=ft.Border.only(bottom=ft.BorderSide(1, "#DEDFD5")),
                        ),
                        ft.Container(content=task_list, padding=ft.Padding.symmetric(horizontal=18)),
                        ft.Container(
                            content=ft.Row(
                                [progress_label, clear_done_button],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            padding=ft.Padding(left=18, right=18, top=8, bottom=8),
                            border=ft.Border.only(top=ft.BorderSide(1, "#DEDFD5")),
                        ),
                    ],
                    spacing=0,
                ),
                width=700,
                bgcolor="#FFFEFA",
                border=ft.Border.all(1, "#E3E3D9"),
                border_radius=8,
                shadow=ft.BoxShadow(blur_radius=28, color="#182D2314", offset=ft.Offset(0, 10)),
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            ),
        ],
        width=700,
        spacing=28,
    )

    app_container = ft.Container(content=content, width=700, padding=ft.Padding.only(top=18, bottom=32))
    workspace_card = content.controls[2]

    def resize_page(event):
        viewport_width = page.width or 748
        page.padding = 12 if viewport_width < 520 else 24
        content_width = min(700, max(272, viewport_width - page.padding * 2))
        app_container.width = content_width
        content.width = content_width
        workspace_card.width = content_width
        page.update()

    page.on_resize = resize_page
    timer_button.on_click = toggle_timer
    page.add(app_container)
    render()
    refresh_timer()
    page.run_task(update_timer)


if __name__ == "__main__":
    ft.run(main, view=ft.AppView.WEB_BROWSER)