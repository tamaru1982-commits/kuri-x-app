"""
confluence_checker.py

crypto_signal_notifier.py(テクニカル)とx_signal_notifier.py(X投稿)、
両方から直近数時間以内に同じ資産・同じ方向のシグナルが出ている場合、
「複数ソースが同じ方向を示している」として強調通知する。

考え方:
単独のシグナルよりも、異なる情報源が同じ結論に達している方が
参考情報としての重みが増す、というシンプルな発想に基づく。
ただし、これも将来の値動きを保証するものではない。
"""

import os
from collections import defaultdict

import requests

import db_utils
import price_utils

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
LOOKBACK_HOURS = int(os.environ.get("CONFLUENCE_LOOKBACK_HOURS", "6"))
COOLDOWN_MINUTES = int(os.environ.get("CONFLUENCE_COOLDOWN_MINUTES", "180"))

SOURCE_NAME = "confluence"
TARGET_SOURCES = ["crypto_technical", "x_post", "whale_flow", "macro_pattern"]


def find_confluences() -> list[dict]:
    rows = db_utils.get_recent_signals(LOOKBACK_HOURS, sources=TARGET_SOURCES)

    # (asset, direction) ごとに、どのソースが含まれるかを集計
    grouped = defaultdict(set)
    for row in rows:
        if row["direction"] not in ("LONG", "SHORT"):
            continue
        grouped[(row["asset"], row["direction"])].add(row["source"])

    confluences = []
    for (asset, direction), sources in grouped.items():
        if len(sources) >= 2:
            confluences.append({"asset": asset, "direction": direction, "sources": sorted(sources)})

    return confluences


def send_discord_notification(item: dict):
    emoji = "🟢" if item["direction"] == "LONG" else "🔴"
    sources_label = " + ".join(item["sources"])
    message = (
        f"⭐ **コンフルエンスアラート** ⭐\n"
        f"{emoji} **{item['asset']}: {item['direction']}**\n"
        f"複数ソースが同方向を示しています: {sources_label}\n"
        f"(直近{LOOKBACK_HOURS}時間以内)\n\n"
        f"_※複数シグナルの一致は参考情報であり、的中を保証するものではありません。_"
    )

    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
    else:
        print(f"[OK] コンフルエンス通知送信: {item['asset']} {item['direction']}")


def main():
    db_utils.init_db()
    confluences = find_confluences()

    if not confluences:
        print("コンフルエンス(複数ソース一致)は見つかりませんでした。")
        return 0

    # 通知対象の銘柄の価格をまとめて1回で取得する(発生時点の価格を残さないと
    # accuracy_tracker/target_trackerが検証対象にできず、的中率が永久に0件になる)
    targets = [
        item for item in confluences
        if not db_utils.was_recently_notified(SOURCE_NAME, item["asset"], item["direction"], COOLDOWN_MINUTES)
    ]
    prices = {}
    if targets:
        try:
            prices = price_utils.fetch_prices(sorted({item["asset"] for item in targets}))
        except Exception as e:
            print(f"[警告] 価格取得に失敗しました(検証対象外として記録します): {e}")

    for item in confluences:
        if db_utils.was_recently_notified(SOURCE_NAME, item["asset"], item["direction"], COOLDOWN_MINUTES):
            print(f"[スキップ] {item['asset']} {item['direction']} はクールダウン中")
            continue

        db_utils.log_signal(SOURCE_NAME, item["asset"], item["direction"], prices.get(item["asset"]),
                             message=f"sources={','.join(item['sources'])}")
        send_discord_notification(item)

    return 0


if __name__ == "__main__":
    main()
