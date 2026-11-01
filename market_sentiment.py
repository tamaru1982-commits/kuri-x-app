"""
market_sentiment.py

市場の過熱感を測る2つの指標を取得して trading_system.db に記録する。
- 資金調達率・建玉: Hyperliquid の公開API(無料・キー不要)。Binance/Bybitの先物APIは
  GitHub Actions(米国)からだと地域制限で弾かれるため、米国からも取れるここを使う。
- 恐怖・強欲指数: alternative.me(無料・キー不要、1日1回更新)

今はダッシュボードに表示するだけで、シグナルの判定には使っていない。
1か月ほど履歴を貯めてから、「過熱時はブレイクを見送る」等のフィルタとして
効くかをバックテストで検証する(効かなければ表示だけに留める)。

dashboard.yml から dashboard_generator.py の前に実行される。取得に失敗しても
ダッシュボード生成は止めたくないため、エラーは警告を出して終了コード0で抜ける。
"""

import os
from datetime import datetime, timedelta, timezone

import requests

import db_utils

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

# Hyperliquidに無い銘柄(EDGE・TRIA)は表示しない
FUNDING_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "SUI", "AAVE", "HYPE"]

# cronが詰まって短い間隔で連続実行されても履歴が偏らないよう、前回からこれ未満なら記録しない
MIN_SNAPSHOT_INTERVAL_MINUTES = int(os.environ.get("FUNDING_MIN_INTERVAL_MINUTES", "50"))
FEAR_GREED_DAYS = 8


def fetch_funding() -> list[dict]:
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=15)
    resp.raise_for_status()
    meta, contexts = resp.json()
    by_name = {u["name"]: c for u, c in zip(meta["universe"], contexts)}
    rows = []
    for asset in FUNDING_ASSETS:
        ctx = by_name.get(asset)
        if not ctx:
            continue
        mark = float(ctx["markPx"])
        rows.append({
            "asset": asset,
            # Hyperliquidは1時間ごとの料率。他の取引所やCoinGlassと比べやすい8時間換算にする
            "funding_8h_pct": float(ctx["funding"]) * 8 * 100,
            "open_interest_usd": float(ctx["openInterest"]) * mark,
            "mark_price": mark,
        })
    return rows


def fetch_fear_greed() -> list[dict]:
    resp = requests.get(FEAR_GREED_URL, params={"limit": FEAR_GREED_DAYS}, timeout=15)
    resp.raise_for_status()
    return [
        {
            "date": datetime.fromtimestamp(int(d["timestamp"]), timezone.utc).strftime("%Y-%m-%d"),
            "value": int(d["value"]),
            "classification": d.get("value_classification"),
        }
        for d in resp.json()["data"]
    ]


def snapshot_due() -> bool:
    last = db_utils.get_last_funding_snapshot_time()
    if not last:
        return True
    elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last)
    return elapsed >= timedelta(minutes=MIN_SNAPSHOT_INTERVAL_MINUTES)


def main():
    db_utils.init_db()

    if snapshot_due():
        try:
            rows = fetch_funding()
            db_utils.add_funding_snapshots(rows)
            print(f"資金調達率を記録しました: {len(rows)}銘柄")
        except Exception as e:
            print(f"::warning::資金調達率の取得に失敗しました: {e}")
    else:
        print("前回の記録から間もないため、資金調達率の記録をスキップしました。")

    try:
        rows = fetch_fear_greed()
        db_utils.upsert_fear_greed(rows)
        print(f"恐怖・強欲指数を記録しました: 最新 {rows[0]['value']} ({rows[0]['classification']})")
    except Exception as e:
        print(f"::warning::恐怖・強欲指数の取得に失敗しました: {e}")


if __name__ == "__main__":
    main()
