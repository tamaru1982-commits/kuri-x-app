"""
position_monitor.py

journal(status='open')に記録されているポジションを監視し、以下のいずれかを
検知したら通知する。

- 損切りライン(記録時に指定したstop_loss)を割り込んだ
- 利確目標(既定+5%)に到達した
- 保有銘柄でcrypto_technicalの新しいSHORT(売り)サインが出た

journal_add.pyで記録した**実ポジション**の場合は、Discordでアラートするのみ
(売るかどうかは人が判断する)。

paper_trader.pyが自動記録した**ペーパートレード**(is_paper=1)の場合は、
人の判断を待たず該当条件でその場で自動決済する。これにより、各シグナル
ソースが実際に儲かる傾向にあるかを実弾なしで継続検証できる。

crypto_signal_notifier.py実行後を追いかける形で、30分おきの実行を想定。

前提:
- journalはLONG(買い)エントリーの記録を想定(SHORTは現物運用のため「売却/見送り」
  の意味で使っており、実際にSHORTポジションを記録することは基本的にない)
- 実ポジションの同一種類の通知は既定12時間のクールダウンで再通知を抑制
  (ペーパートレードは決済後status='closed'になるため、再チェック対象から自然に外れる)

注意:
- 価格変動の方向・比率のみに基づく単純な判定です。投資助言ではありません。
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

import requests

import db_utils

TARGET_PROFIT_PCT = float(os.environ.get("POSITION_TARGET_PROFIT_PCT", "5.0"))
ALERT_COOLDOWN_HOURS = float(os.environ.get("POSITION_ALERT_COOLDOWN_HOURS", "12"))
TECH_SIGNAL_LOOKBACK_HOURS = float(os.environ.get("POSITION_TECH_LOOKBACK_HOURS", "2"))

# ペーパートレードの決済価格に往復分の想定手数料を反映し、成績が実態より
# 良く見えすぎないようにする(片道0.15%を想定。国内取引所の現物手数料の目安)。
PAPER_FEE_PCT = float(os.environ.get("PAPER_FEE_PCT", "0.15"))

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
    if resp.status_code == 429:
        for _ in range(2):
            time.sleep(30)
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 429:
                break
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


def build_paper_close_message(row, market_price: float, fee_adjusted_pnl_pct: float, alert_types: list[str]) -> str:
    """ペーパートレード(is_paper=1)は人の判断を待たず自動決済するため、
    「ご検討ください」ではなく「決済しました」の報告として通知する。
    損益は往復手数料(既定0.3%)を差し引いた実態に近い値を表示する。"""
    lines = [f"**📝 ペーパートレード決済: {row['asset']}**", ""]
    lines.append(f"建値 ${row['entry_price']:,.4f} → 市場価格 ${market_price:,.4f}")
    lines.append(f"手数料(往復{PAPER_FEE_PCT * 2:.2f}%)考慮後 損益: {fee_adjusted_pnl_pct:+.2f}%")
    if row["source"]:
        lines.append(f"シグナル元: {row['source']}")
    lines.append("")

    if "take_profit" in alert_types:
        lines.append(f"🟢 利確目安(+{TARGET_PROFIT_PCT:.1f}%)到達で決済")
    if "stop_loss" in alert_types:
        lines.append("🔴 損切りライン到達で決済")
    if "tech_short" in alert_types:
        lines.append("⚠️ テクニカル売りサイン発生で決済")

    lines.append("")
    lines.append(
        f"_※実際の売買ではない自動シミュレーションです(journal id={row['id']})。"
        f"シグナルソース別の実力を検証するための記録です。投資助言ではありません。_"
    )
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
        save_state(state)  # state_fileが未作成だとワークフロー側のgit addが失敗するため必ず保存する
        return 0

    price_cache: dict[str, float | None] = {}

    for row in positions:
        asset = row["asset"]

        if asset not in price_cache:
            try:
                price_cache[asset] = get_current_price(asset)
            except Exception as e:
                print(f"[エラー] {asset} の価格取得に失敗しました: {e}")
                price_cache[asset] = None
            time.sleep(2)  # 銘柄ごとに1回だけ取得し、CoinGeckoのレート制限を避ける

        current_price = price_cache[asset]
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

            if row["is_paper"]:
                # ペーパートレードは人の判断を待たず、条件を満たした時点で自動決済する。
                # 記録するexit_priceには往復手数料を織り込み、成績が実態より良く見えないようにする。
                fee_multiplier = 1 - (2 * PAPER_FEE_PCT / 100)
                if row["direction"] == "LONG":
                    fee_adjusted_exit = current_price * fee_multiplier
                    fee_adjusted_pnl_pct = (fee_adjusted_exit - row["entry_price"]) / row["entry_price"] * 100
                else:
                    fee_adjusted_exit = current_price / fee_multiplier
                    fee_adjusted_pnl_pct = (row["entry_price"] - fee_adjusted_exit) / row["entry_price"] * 100

                db_utils.close_journal_entry(row["id"], fee_adjusted_exit)
                send_discord_notification(build_paper_close_message(row, current_price, fee_adjusted_pnl_pct, alert_types))
                print(f"[ペーパー決済] {asset} (id={row['id']}, source={row['source']}): "
                      f"{','.join(alert_types)} / 手数料考慮後損益{fee_adjusted_pnl_pct:+.2f}%(市場{pnl_pct:+.2f}%)")
            else:
                send_discord_notification(build_alert_message(row, current_price, pnl_pct, alert_types))
                print(f"[通知] {asset} (id={row['id']}): {','.join(alert_types)} / 含み損益{pnl_pct:+.2f}%")
        else:
            kind = "ペーパー" if row["is_paper"] else "実"
            print(f"{asset} (id={row['id']}, {kind}): 含み損益{pnl_pct:+.2f}% (通知条件なし)")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
