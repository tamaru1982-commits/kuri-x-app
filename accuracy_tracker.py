"""
accuracy_tracker.py

crypto_signal_notifier.py が記録したシグナル(LONG/SHORT)について、
発生から約1時間後・約24時間後の価格を取得し、的中/不的中を判定してDBに記録する。
さらに、直近の的中率サマリーをDiscordに定期報告する。

前提:
- 価格追跡はCoinGecko(仮想通貨)を想定。price_at_signalが記録されているシグナルのみ対象。
- このスクリプトは1〜数時間おきに実行する想定(GitHub Actionsのcronで管理)。

判定窓について:
判定対象は「発生からN時間経過したもの」という時間窓で絞っているため、窓の間に
実行が失敗し続けると、そのシグナルの判定は二度と行われない(永久欠損になる)。
これを避けるため、
- 価格取得は銘柄ごとにまとめて1回だけ行い(price_utils.fetch_prices)、
  レート制限のリトライも共通化する
- 1件の失敗で全体を落とさず、取得できた分だけ確実に記録する
- 時間窓に余裕を持たせる(1回取りこぼしても次の実行で拾えるようにする)
の3点で守っている。
"""

import os
from datetime import datetime, timezone

import requests

import db_utils
import price_utils

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# 週次サマリーをどの曜日に送るか(0=月曜 ... 6=日曜)。Noneなら毎回送る。
SUMMARY_WEEKDAY = int(os.environ.get("SUMMARY_WEEKDAY", "0"))  # デフォルト: 月曜


def record_pending(min_age_hours: float, max_age_hours: float,
                   outcome_field: str, price_field: str, label: str):
    """指定した時間窓の未判定シグナルについて、現在価格を取得して的中/不的中を記録する。
    必要な銘柄の価格は1回のリクエストでまとめて取得し、レート制限を受けにくくする。"""
    pending = db_utils.get_pending_outcome_signals(
        min_age_hours=min_age_hours, max_age_hours=max_age_hours, outcome_field=outcome_field
    )
    if not pending:
        return

    symbols = sorted({row["asset"] for row in pending if price_utils.is_supported(row["asset"])})
    try:
        prices = price_utils.fetch_prices(symbols)
    except Exception as e:
        # ここで例外を投げるとワークフロー自体が落ち、DBのコミット手順まで到達しない。
        # 判定窓を逃すと永久欠損になるため、失敗しても次回に賭けて処理を続行する。
        print(f"[エラー] {label}: 価格取得に失敗したためスキップします: {e}")
        return

    for row in pending:
        price_now = prices.get(row["asset"])
        if price_now is None:
            print(f"[スキップ] {label} {row['asset']}: 価格取得不可(対応表未登録の可能性)")
            continue
        db_utils.record_outcome(row["id"], price_field, outcome_field, price_now)
        print(f"[{label}] {row['asset']} {row['direction']} -> 記録済み")


def check_outcomes():
    # 1時間後チェック(50分〜3時間経過したものを対象。1回失敗しても次回に拾えるよう幅を持たせる)
    record_pending(50 / 60, 3, "outcome_1h", "price_1h", "1h検証")

    # 24時間後チェック(22時間〜30時間経過したものを対象)
    record_pending(22, 30, "outcome_24h", "price_24h", "24h検証")


def build_summary_message() -> str | None:
    summary = db_utils.get_hit_rate_summary(hours=24 * 30)  # 直近30日
    if not summary:
        return None

    lines = ["**📊 シグナル的中率レポート(直近30日・24時間後判定)**", ""]
    for row in summary:
        lines.append(
            f"・{row['source']} / {row['asset']}: "
            f"的中率 {row['hit_rate_pct']}% ({row['correct']}/{row['total']}件)"
        )
    lines.append("")
    lines.append("_※価格変動の方向のみで判定した簡易的中率です。値幅や手数料は考慮していません。_")
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
        print("[OK] 的中率レポートを送信しました。")


def main():
    db_utils.init_db()
    check_outcomes()

    today_weekday = datetime.now(timezone.utc).weekday()
    if today_weekday != SUMMARY_WEEKDAY:
        print("本日はサマリー送信日ではありません。")
        return 0

    message = build_summary_message()
    if message:
        send_discord_notification(message)
    else:
        print("まだ的中率を計算できるデータがありません。")
    return 0


if __name__ == "__main__":
    main()
