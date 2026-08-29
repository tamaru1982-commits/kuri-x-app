"""
paper_trader.py

各シグナルソース(tech/dip/whale/confluence/macro)からLONGが発生するたびに、
実際には買わず「もし毎回シグナル通りに買っていたら」を検証するための
仮想ポジション(ペーパートレード)を自動でjournalに記録する。

実際にお金を使わずに、どのシグナルソースが実際に儲かる傾向にあるのかを
継続的に検証できるようにするための仕組み。実運用(手動記録)とは
journal.is_paper列で区別され、混同しない。

決済(利確/損切り/テクニカル悪化での自動クローズ)はposition_monitor.pyが
既存の監視ロジックをそのままペーパートレードにも適用する形で行う。

前提:
- 直近処理したsignals.idを状態ファイルに保持し、新規のLONGシグナルのみを処理する
- price_at_signalが記録されていないソース(whale/confluence/macro)は、
  その時点の価格をCoinGeckoから取得する
- 対応表に無い資産(ステーブルコイン・「市場全体」等)はペーパートレード対象外

注意:
- 実際の売買ではありません。手数料・スリッページ・約定タイミングは考慮していません。
  あくまで「シグナルに機械的に従った場合の参考シミュレーション」です。投資助言ではありません。
"""

import os
import sys
import json
import time
from pathlib import Path

import requests

import db_utils
import risk_utils

NOTIONAL_USD = float(os.environ.get("PAPER_NOTIONAL_USD", "100"))  # 1トレードあたりの想定金額
STOP_LOSS_PCT = risk_utils.DEFAULT_STOP_LOSS_PCT

STATE_FILE = Path("paper_trader_state.json")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

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


def get_new_long_signals(since_id: int) -> list:
    conn = db_utils.get_conn()
    rows = conn.execute(
        "SELECT * FROM signals WHERE id > ? AND direction = 'LONG' ORDER BY id ASC",
        (since_id,),
    ).fetchall()
    conn.close()
    return rows


def main():
    db_utils.init_db()
    state = load_state()
    last_id = state.get("last_signal_id", 0)

    signals = get_new_long_signals(last_id)
    if not signals:
        print("新規のLONGシグナルはありません。")
        return 0

    opened = 0
    max_id = last_id

    for sig in signals:
        max_id = max(max_id, sig["id"])
        asset = sig["asset"]

        if asset not in SYMBOL_TO_COINGECKO_ID:
            print(f"[スキップ] {sig['source']} {asset}: 対応表未登録のため対象外")
            continue

        price = sig["price_at_signal"]
        if price is None:
            try:
                price = get_current_price(asset)
                time.sleep(2)
            except Exception as e:
                print(f"[エラー] {asset} の価格取得に失敗しました: {e}")
                continue

        if price is None or price <= 0:
            print(f"[スキップ] {sig['source']} {asset}: 価格取得不可")
            continue

        size = NOTIONAL_USD / price
        stop_loss = price * (1 - STOP_LOSS_PCT / 100)

        db_utils.add_journal_entry(
            asset=asset, direction="LONG", entry_price=price, size=size, stop_loss=stop_loss,
            note=f"paper:{sig['source']}", is_paper=True, source=sig["source"],
        )
        opened += 1
        print(f"[OK] ペーパー建玉: {sig['source']} {asset} @ ${price:,.4f}(想定${NOTIONAL_USD:.0f}分)")

    state["last_signal_id"] = max_id
    save_state(state)
    print(f"{opened}件のペーパートレードを新規記録しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
