"""
journal_report.py

journal_add.py / journal_close.py で記録したトレード履歴の成績
(勝率・合計損益・平均利益/損失)を集計し、Discordに通知する。

ローカルで手動実行してもよいし、週次でGitHub Actionsから実行してもよい。
"""

import os

import requests

import db_utils

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def build_message() -> str:
    summary = db_utils.get_journal_summary()

    if summary["total_trades"] == 0:
        return "**📒 トレード日誌レポート**\n\nまだ決済済みのトレード記録がありません。"

    lines = [
        "**📒 トレード日誌レポート**",
        "",
        f"決済済みトレード数: {summary['total_trades']}件",
        f"勝率: {summary['win_rate_pct']}% ({summary['win_count']}勝{summary['loss_count']}敗)",
        f"合計損益: {summary['total_pnl']:+.2f}",
        f"平均利益(勝ちトレード): {summary['avg_win']:+.2f}",
        f"平均損失(負けトレード): {summary['avg_loss']:+.2f}",
    ]
    return "\n".join(lines)


def send_discord_notification(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
    else:
        print("[OK] トレード日誌レポートを送信しました。")


def main():
    db_utils.init_db()
    message = build_message()
    print(message)
    send_discord_notification(message)


if __name__ == "__main__":
    main()
