"""
target_tracker.py

「シグナル発生から1〜3日以内に、指定した目標%(既定+5%)に到達したか」を
価格推移全体(経路)から追跡する。accuracy_tracker.py(1時間後・24時間後の
2時点のスナップショットのみを見る)とは別に、目標達成型の短期トレード戦略
(例: 10万円分現物を買って、+5,000円[+5%]になったら売る、1〜3日以内)を
検証するためのもの。

目標に到達したかどうかに加え、到達までの時間・到達前の最大逆行幅(含み損の
最大値)も記録する。最大逆行幅が分かれば、「この損切り幅だと目標到達前に
切られていたはず」といった判断が事後的にできるようになる。

前提:
- crypto_technical / crypto_dip のように price_at_signal が記録されているシグナルが対象
  (X投稿・クジラ・マクロ相関は価格を記録していないため対象外)
- 1回の実行で、未確定(target_hit IS NULL)なシグナルについて、目標に到達したか、
  または判定期間(既定72時間)が経過して未到達が確定したかをチェックする

注意:
- 方向のみの単純な経路追跡であり、実際の売買(手数料・スリッページ・約定タイミング)
  を反映したものではありません。投資助言ではありません。
"""

import os
import sys
import time
from datetime import datetime

import requests
import pandas as pd

import db_utils

TARGET_PCT = float(os.environ.get("TARGET_PCT", "5.0"))
TARGET_WINDOW_HOURS = float(os.environ.get("TARGET_WINDOW_HOURS", "72"))

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# crypto_signal_notifier.py / accuracy_tracker.py と同じ対応表
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "HYPE": "hyperliquid", "DOGE": "dogecoin", "EDGE": "edgex", "TRIA": "tria",
    "SUI": "sui", "AAVE": "aave",
}


def fetch_price_series(coin_id: str) -> pd.Series:
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": 7}
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code == 429:
        for _ in range(2):
            time.sleep(30)
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 429:
                break
    resp.raise_for_status()
    prices = resp.json().get("prices", [])
    df = pd.DataFrame(prices, columns=["timestamp_ms", "price"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df.set_index("timestamp")["price"].sort_index()


def get_pending_target_signals():
    conn = db_utils.get_conn()
    placeholders = ",".join("?" for _ in SYMBOL_TO_COINGECKO_ID)
    rows = conn.execute(
        f"SELECT * FROM signals WHERE target_hit IS NULL AND price_at_signal IS NOT NULL "
        f"AND direction IN ('LONG', 'SHORT') AND asset IN ({placeholders})",
        list(SYMBOL_TO_COINGECKO_ID.keys()),
    ).fetchall()
    conn.close()
    return rows


def evaluate_signal(row, price_series: pd.Series) -> dict | None:
    signal_time_naive = datetime.fromisoformat(row["timestamp"])
    signal_time = pd.Timestamp(signal_time_naive).tz_localize("UTC")
    price_at_signal = row["price_at_signal"]
    direction = row["direction"]

    after = price_series[price_series.index >= signal_time]
    if after.empty:
        return None

    # LONGなら上昇率、SHORTなら下落率を「有利方向への変化率」として揃える
    pct_change = (after - price_at_signal) / price_at_signal * 100
    favorable = pct_change if direction == "LONG" else -pct_change

    elapsed_hours = (datetime.utcnow() - signal_time_naive).total_seconds() / 3600
    target_pct = row["target_pct"] or TARGET_PCT
    window_hours = row["target_window_hours"] or TARGET_WINDOW_HOURS

    hit_mask = favorable >= target_pct
    if hit_mask.any():
        hit_index = favorable[hit_mask].index[0]
        hit_hours = (hit_index - signal_time).total_seconds() / 3600
        before_hit = favorable[favorable.index <= hit_index]
        worst = before_hit.min()
        max_adverse = round(-worst, 2) if worst < 0 else 0.0
        return {"target_hit": "yes", "target_hit_hours": round(hit_hours, 1), "max_adverse_pct": max_adverse}

    if elapsed_hours >= window_hours:
        worst = favorable.min()
        max_adverse = round(-worst, 2) if worst < 0 else 0.0
        return {"target_hit": "no", "target_hit_hours": None, "max_adverse_pct": max_adverse}

    return None  # まだ判定できない(未達だが判定期間内)


def record_result(signal_id: int, result: dict):
    conn = db_utils.get_conn()
    conn.execute(
        "UPDATE signals SET target_hit = ?, target_hit_hours = ?, max_adverse_pct = ?, "
        "target_pct = COALESCE(target_pct, ?), target_window_hours = COALESCE(target_window_hours, ?) "
        "WHERE id = ?",
        (result["target_hit"], result["target_hit_hours"], result["max_adverse_pct"],
         TARGET_PCT, TARGET_WINDOW_HOURS, signal_id),
    )
    conn.commit()
    conn.close()


def main():
    db_utils.init_db()

    pending = get_pending_target_signals()
    if not pending:
        print("判定対象のシグナルはありません。")
        return 0

    by_asset: dict[str, list] = {}
    for row in pending:
        by_asset.setdefault(row["asset"], []).append(row)

    for asset, rows in by_asset.items():
        coin_id = SYMBOL_TO_COINGECKO_ID.get(asset)
        if not coin_id:
            continue
        try:
            price_series = fetch_price_series(coin_id)
        except Exception as e:
            print(f"[エラー] {asset} の価格取得に失敗: {e}")
            continue

        for row in rows:
            result = evaluate_signal(row, price_series)
            if result:
                record_result(row["id"], result)
                if result["target_hit"] == "yes":
                    status = f"到達({result['target_hit_hours']}時間後)"
                else:
                    status = "未到達(判定期間終了)"
                print(f"[判定] {asset} {row['direction']} (id={row['id']}): {status} / "
                      f"到達前最大逆行 {result['max_adverse_pct']}%")

        time.sleep(6)

    return 0


if __name__ == "__main__":
    sys.exit(main())
