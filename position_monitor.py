"""
position_monitor.py

journal(journal_add.pyで記録した保有中ポジション、status='open')を監視し、
以下のいずれかを検知したらDiscordに通知する。

- 損切りライン(記録時に指定したstop_loss)を割り込んだ
- 利確目標(既定+5%)に到達した
- 保有銘柄でcrypto_technicalの新しいSHORT(売り)サインが出た

記録した建値・損切りラインを「記録するだけ」で終わらせず、実際の売り時判断に
活かすための機能。crypto_signal_notifier.py実行後を追いかける形で、
30分おきの実行を想定。

前提:
- journalはLONG(買い)エントリーの記録を想定(SHORTは現物運用のため「売却/見送り」
  の意味で使っており、実際にSHORTポジションを記録することは基本的にない)
- 同一ポジション・同一種類の通知は既定12時間のクールダウンで再通知を抑制

注意:
- 価格変動の方向・比率のみに基づく単純な判定です。投資助言ではありません。
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

import requests

import db_utils

TARGET_PROFIT_PCT = float(os.environ.get("POSITION_TARGET_PROFIT_PCT", "5.0"))
ALERT_COOLDOWN_HOURS = float(os.environ.get("POSITION_ALERT_COOLDOWN_HOURS", "12"))
TECH_SIGNAL_LOOKBACK_HOURS = float(os.environ.get("POSITION_TECH_LOOKBACK_HOURS", "2"))

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
STATE_FILE = Path("position_monitor_state.json")

# crypto_signal_notifier.py / accuracy_tracker.py / target_tracker.py と同じ対応表
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "HYPE": "hyperliquid", "DOGE": "dogecoin", "EDGE": "edgex", "TRIA": "tria",
    "SUI": "sui", "AAVE": "aave",
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def get_current_price(symbol: str) -> float | None:
    coin_id = SYMBOL_TO_COINGECKO_ID.get(symbol)
    if not coin_id:
        return None
    url = f"{COINGECKO_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get(coin_id, {}).get("usd")


def has_recent_short_signal(asset: str, hours: float) -> bool:
    conn = db_utils.get_conn()
    row = conn.execute(
        "SELECT 1 FROM signals WHERE source = 'crypto_technical' AND asset = ? AND direction = 'SHORT' "
        "AND datetime(timestamp) >= datetime('now', ?) LIMIT 1",
        (asset, f"-{hours} hours"),
    ).fetchone()
    conn.close()
    return row is not None


def was_recently_alerted(state: dict, journal_id: int, alert_type: str, cooldown_hours: float) -> bool:
    last = state.get(f"{journal_id}:{alert_type}")
    if not last:
        return False
    elapsed_hours = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 3600
    return elapsed_hours < cooldown_hours


def mark_alerted(state: dict, journal_id: int, alert_type: str):
    state[f"{journal_id}:{alert_type}"] = datetime.utcnow().isoformat()


def build_alert_message(row, current_price: float, pnl_pct: float, alert_types: list[str]) -> str:
    lines = [f"**📌 保有ポジション通知: {row['asset']}**", ""]
    lines.append(f"建値 ${row['entry_price']:,.4f} → 現在値 ${current_price:,.4f}({pnl_pct:+.2f}%)")
    if row["stop_loss"] is not None:
        lines.append(f"損切りライン: ${row['stop_loss']:,.4f}")
    lines.append("")

    if "take_profit" in alert_types:
        lines.append(f"🟢 **利確目安(+{TARGET_PROFIT_PCT:.1f}%)に到達しています。**")
    if "stop_loss" in alert_types:
        lines.append("🔴 **損切りラインを割り込んでいます。**")
    if "tech_short" in alert_types:
        lines.append("⚠️ この銘柄でテクニカル的に売り(SHORT)サインも出ています。")

    if row["note"]:
        lines.append(f"\nメモ: {row['note']}")

    lines.append("")
    lines.append(f"_※記録したポジション(journal id={row['id']})に対する参考通知です。投資助言ではありません。_")
    return "\n".join(lines)


def send_discord_notification(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL が設定されていないため、コンソールに出力のみ行います。")
        print(message)
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知に失敗しました: {resp.status_code} {resp.text}")
    else:
        print("[OK] ポジション監視通知を送信しました。")


def main():
    db_utils.init_db()
    state = load_state()

    positions = db_utils.get_open_positions()
    if not positions:
        print("保有中のポジションはありません。")
        return 0

    for row in positions:
        asset = row["asset"]
        try:
            current_price = get_current_price(asset)
        except Exception as e:
            print(f"[エラー] {asset} の価格取得に失敗しました: {e}")
            continue

        if current_price is None:
            print(f"[スキップ] {asset}: 価格取得不可(対応表未登録の可能性)")
            continue

        if row["direction"] == "LONG":
            pnl_pct = (current_price - row["entry_price"]) / row["entry_price"] * 100
        else:
            pnl_pct = (row["entry_price"] - current_price) / row["entry_price"] * 100

        alert_types = []

        if pnl_pct >= TARGET_PROFIT_PCT and not was_recently_alerted(state, row["id"], "take_profit", ALERT_COOLDOWN_HOURS):
            alert_types.append("take_profit")

        if row["stop_loss"] is not None:
            stop_breached = (
                current_price <= row["stop_loss"] if row["direction"] == "LONG"
                else current_price >= row["stop_loss"]
            )
            if stop_breached and not was_recently_alerted(state, row["id"], "stop_loss", ALERT_COOLDOWN_HOURS):
                alert_types.append("stop_loss")

        if row["direction"] == "LONG" and has_recent_short_signal(asset, TECH_SIGNAL_LOOKBACK_HOURS):
            if not was_recently_alerted(state, row["id"], "tech_short", ALERT_COOLDOWN_HOURS):
                alert_types.append("tech_short")

        if alert_types:
            for alert_type in alert_types:
                mark_alerted(state, row["id"], alert_type)
            send_discord_notification(build_alert_message(row, current_price, pnl_pct, alert_types))
            print(f"[通知] {asset} (id={row['id']}): {','.join(alert_types)} / 含み損益{pnl_pct:+.2f}%")
        else:
            print(f"{asset} (id={row['id']}): 含み損益{pnl_pct:+.2f}% (通知条件なし)")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
