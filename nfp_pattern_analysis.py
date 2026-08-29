"""
nfp_pattern_analysis.py

過去の米雇用統計(NFP)発表について「前月比の増減が直近3ヶ月平均と比べて
上振れ/下振れだったか」で発表を分類し、各分類ごとに主要資産の値動き
(発表前日終値→発表当日終値)の平均変化率・上昇確率を集計して
nfp_patterns.json に保存するスクリプト。

ローカルで手動実行する想定(月1回程度の更新を推奨)。
economic_calendar_reminder.py がこの出力ファイルを読み込んで通知に添付する。

前提:
- FRED APIキーが必要(無料・即時発行 https://fred.stlouisfed.org/ )
- 価格データはyfinanceを使用(無料)

注意:
- FREDには「市場予想(コンセンサス)」のデータが無いため、
  代理指標として「直近3ヶ月平均との比較」で上振れ/下振れを判定している(簡易的な近似)。
- 発表日とNFP実測値(PAYEMS)の対応付けは、両者を新しい順に並べて突き合わせる近似的な方法。
  ベンチマーク改定等が挟まると厳密には1対1にならない場合がある。
- あくまで過去の統計的傾向の集計であり、将来の発表結果や値動きを予測・保証するものではありません。
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"
NFP_RELEASE_ID = 50
NFP_SERIES_ID = "PAYEMS"

LOOKBACK_RELEASES = int(os.environ.get("NFP_LOOKBACK_RELEASES", "24"))  # 直近何回分を集計するか
OUTPUT_FILE = Path("nfp_patterns.json")

# 値動きを追跡する資産(yfinanceティッカー)。必要に応じて追加・変更してください。
TARGETS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}


def fetch_release_dates(limit: int) -> list[str]:
    url = f"{FRED_BASE}/release/dates"
    params = {
        "release_id": NFP_RELEASE_ID, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "desc", "include_release_dates_with_no_data": "false",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    dates = [d["date"] for d in resp.json().get("release_dates", [])]
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [d for d in dates if d < today]
    return dates[:limit]


def fetch_payems_observations(limit: int) -> pd.DataFrame:
    url = f"{FRED_BASE}/series/observations"
    params = {
        "series_id": NFP_SERIES_ID, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "desc",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    df = pd.DataFrame(obs)
    df = df[df["value"] != "."]
    df["value"] = df["value"].astype(float)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    return df.head(limit + 4)  # 前月比・直近3ヶ月平均の計算用に少し多めに取得


def build_release_records(limit: int) -> list[dict]:
    """発表日と、その回の前月比変化・直近3ヶ月平均との比較を突き合わせる。"""
    release_dates = fetch_release_dates(limit)
    payems = fetch_payems_observations(limit)

    payems = payems.sort_values("date").reset_index(drop=True)
    payems["change"] = payems["value"].diff()
    payems_desc = payems.sort_values("date", ascending=False).reset_index(drop=True)

    records = []
    for i, release_date in enumerate(release_dates):
        if i >= len(payems_desc):
            break
        change = payems_desc.iloc[i]["change"]
        if pd.isna(change):
            continue

        prior_changes = payems_desc.iloc[i + 1: i + 4]["change"].dropna()
        if len(prior_changes) < 3:
            continue
        baseline = prior_changes.mean()

        bucket = "上振れ(直近3ヶ月平均超)" if change > baseline else "下振れ(直近3ヶ月平均以下)"
        records.append({"release_date": release_date, "change": change, "baseline": baseline, "bucket": bucket})

    return records


def fetch_price_move(ticker: str, release_date: str) -> float | None:
    """発表前日終値 -> 発表当日終値の変化率(%)。データが無ければNone。"""
    release_dt = datetime.strptime(release_date, "%Y-%m-%d")
    start = release_dt - timedelta(days=7)
    end = release_dt + timedelta(days=2)

    hist = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    if hist.empty:
        return None

    hist = hist.sort_index()
    if hist.index.tz is not None:
        hist.index = hist.index.tz_convert(None)

    before = hist[hist.index.date < release_dt.date()]
    on_or_after = hist[hist.index.date >= release_dt.date()]
    if before.empty or on_or_after.empty:
        return None

    price_before = before["Close"].iloc[-1]
    price_after = on_or_after["Close"].iloc[0]
    if price_before == 0:
        return None
    return (price_after - price_before) / price_before * 100


def summarize(records: list[dict]) -> dict:
    for rec in records:
        rec["moves"] = {}
        for label, ticker in TARGETS.items():
            rec["moves"][label] = fetch_price_move(ticker, rec["release_date"])

    patterns = {}
    for bucket in sorted(set(r["bucket"] for r in records)):
        patterns[bucket] = {}
        bucket_records = [r for r in records if r["bucket"] == bucket]
        for label in TARGETS:
            moves = [r["moves"][label] for r in bucket_records if r["moves"].get(label) is not None]
            if not moves:
                continue
            avg_move = sum(moves) / len(moves)
            up_ratio = sum(1 for m in moves if m > 0) / len(moves) * 100
            patterns[bucket][label] = {
                "avg_move_pct": round(avg_move, 2),
                "up_ratio_pct": round(up_ratio, 1),
                "sample_count": len(moves),
            }

    return patterns


def main():
    if not FRED_API_KEY:
        print("[エラー] FRED_API_KEY が設定されていません。")
        return 1

    print(f"直近{LOOKBACK_RELEASES}回分のNFP発表を集計します...")
    records = build_release_records(LOOKBACK_RELEASES)
    print(f"{len(records)}件の発表データを取得しました。価格データを取得中...")

    patterns = summarize(records)

    output = {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "lookback_releases": LOOKBACK_RELEASES,
        "patterns": patterns,
    }
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {OUTPUT_FILE} を生成しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
