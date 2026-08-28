"""
accuracy_tracker.py

crypto_signal_notifier.py が記録したシグナル(LONG/SHORT)について、
発生から約1時間後・約24時間後の価格を取得し、的中/不的中を判定してDBに記録する。
さらに、直近の的中率サマリーをDiscordに定期報告する。

前提:
- 価格追跡はCoinGecko(仮想通貨)を想定。priceが記録されているシグナル(=crypto_technical)のみ対象。
  X投稿シグナルは価格が紐付いていないため、的中率検証の対象外(将来的に拡張可能)。
- このスクリプトは1〜数時間おきに実行する想定(GitHub Actionsのcronで管理)。
"""

import os
from datetime import datetime, timezone

import requests

import db_utils

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# symbol -> coingecko id の対応(crypto_signal_notifier.pyのCOINSと合わせる)
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "HYPE": "hyperliquid",
    "DOGE": "dogecoin",
    "EDGE": "edgex",
    "TRIA": "tria",
    "SUI": "sui",
    "AAVE": "aave",
}

# 週次サマリーをどの曜日に送るか(0=月曜 ... 6=日曜)。Noneなら毎回送る。
SUMMARY_WEEKDAY = int(os.environ.get("SUMMARY_WEEKDAY", "0"))  # デフォルト: 月曜


def get_current_price(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_COINGECKO_ID.get(symbol)
    if not coin_id:
        return None

    url = f"{COINGECKO_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get(coin_id, {}).get("usd")


def check_outcomes():
    # 1時間後チェック(50分〜90分経過したものを対象)
    pending_1h = db_utils.get_pending_outcome_signals(min_age_hours=50 / 60, max_age_hours=90 / 60, outcome_field="outcome_1h")
    for row in pending_1h:
        price_now = get_current_price(row["asset"])
        if price_now is not None:
            db_utils.record_outcome(row["id"], "price_1h", "outcome_1h", price_now)
            print(f"[1h検証] {row['asset']} {row['direction']} -> 記録済み")

    # 24時間後チェック(22時間〜26時間経過したものを対象)
    pending_24h = db_utils.get_pending_outcome_signals(min_age_hours=22, max_age_hours=26, outcome_field="outcome_24h")
    for row in pending_24h:
        price_now = get_current_price(row["asset"])
        if price_now is not None:
            db_utils.record_outcome(row["id"], "price_24h", "outcome_24h", price_now)
            print(f"[24h検証] {row['asset']} {row['direction']} -> 記録済み")


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
