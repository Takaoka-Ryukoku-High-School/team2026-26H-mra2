# Game Time Manager

ゲーム依存症対策向けの時間管理アプリです。1日のゲーム上限と1週間の上限を設定し、時間を超えると通知で知らせて、アプリ側で新しいゲーム開始を止めます。過去7日間の使用量をグラフで確認でき、使いすぎを可視化します。

## 主な機能

- 1日の上限設定（時間）
- 1週間の上限設定（時間）
- 現在の使用時間と残り時間の表示
- 80%到達時の通知
- 上限到達時の自動停止と通知
- 過去7日分の使用量グラフ
- SQLite によるローカル保存

## 起動方法

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Linux では `notify-send` が入っていると、上限到達時にデスクトップ通知が出ます。`notify-send` が使えない環境では、ターミナルにメッセージが表示されます。