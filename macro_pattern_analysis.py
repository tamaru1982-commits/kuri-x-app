"""
macro_pattern_analysis.py

雇用統計(NFP)・金(Gold)・ドル指数(DXY)・AI株(NVDA)・S&P500・VIX(恐怖指数)・
原油(WTI)・FF金利(FOMC政策金利)の月次の値動きの組み合わせパターンと、
その翌月のBTC値動きの過去傾向を集計して macro_patterns.json に保存する。
ローカルで手動実行する想定(月1回程度の更新を推奨)。macro_signal_notifier.pyが
この出力を読み込んでシグナル判定に使う。

考え方(2要因への単純化):
- 要因A: 雇用統計(NFP)が直近3ヶ月平均と比べて上振れ/下振れだったか
- 要因B: 金・ドル指数・AI株(NVDA)・S&P500・VIX・原油・FF金利の当月の動きから
  算出する簡易リスクセンチメント(多数決で「リスクオン」/「リスクオフ」)
  各指標を独立にバケット分けすると1バケットあたりのサンプル数が少なくなりすぎるため、
  全て1つの合成スコアにまとめている(要因A×要因Bの2×2=4パターンに集約)

リスクセンチメント判定の符号の向き:
- NVDA上昇 / S&P500上昇 → リスクオン方向(+1)
- 金上昇 / ドル上昇 / VIX上昇 → リスクオフ方向(逆相関の安全資産・恐怖指数のため、
  上昇でリスクオフに投票)
- 原油上昇 → リスクオフ方向(急激な原油高はインフレ懸念・景気減速リスクとして
  扱う簡易的な割り切り。実際は需要主導の上昇ならリスクオンとも解釈できるため、
  この符号付けはあくまで単純化した経験則)
- FF金利(FOMC政策金利)が前月比で上昇(利上げ) → リスクオフ方向、
  低下(利下げ) → リスクオン方向、変化なしなら投票しない

前提:
- FRED APIキーが必要(無料・即時発行)
- 価格データはyfinanceを使用(無料)

注意:
- 暗号資産の価格データは2014年頃までしか遡れないため、"2012年まで"の分析はできない
  (取得できる最古のデータから集計する)
- 月次×4パターンのため、1パターンあたりのサンプル数はせいぜい数十件程度に留まる。
  過去の統計的傾向の集計であり、将来を保証するものではない。サンプル数が少ない
  パターンは参考程度に留めること。投資助言ではありません。
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd
import yfinance as yf

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"
NFP_SERIES_ID = "PAYEMS"
FEDFUNDS_SERIES_ID = "FEDFUNDS"  # 実効FF金利(FOMC政策金利の実勢値)、月次

OUTPUT_FILE = Path("macro_patterns.json")

TICKERS = {
    "gold": "GC=F",
    "dollar": "DX-Y.NYB",
    "nvda": "NVDA",
    "sp500": "^GSPC",
    "vix": "^VIX",
    "oil": "CL=F",
    "btc": "BTC-USD",
}

REQUIRED_COLUMNS = [
    "payems_change", "gold_ret", "dollar_ret", "nvda_ret",
    "sp500_ret", "vix_ret", "oil_ret", "fedfunds_change", "btc_ret",
]


def fetch_fred_monthly(series_id: str) -> pd.Series:
    url = f"{FRED_BASE}/series/observations"
    params = {"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "asc"}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    df = pd.DataFrame(obs)
    df = df[df["value"] != "."]
    df["value"] = df["value"].astype(float)
    df["month"] = pd.to_datetime(df["date"]).dt.to_period("M")
    return df.set_index("month")["value"]


def fetch_monthly_close(ticker: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period="max", interval="1mo")
    if hist.empty:
        return pd.Series(dtype=float)
    hist.index = hist.index.to_period("M")
    close = hist["Close"]
    return close[~close.index.duplicated(keep="last")]


def build_monthly_table() -> pd.DataFrame:
    payems_change = fetch_fred_monthly(NFP_SERIES_ID).diff()
    fedfunds_change = fetch_fred_monthly(FEDFUNDS_SERIES_ID).diff()

    gold_ret = fetch_monthly_close(TICKERS["gold"]).pct_change() * 100
    dollar_ret = fetch_monthly_close(TICKERS["dollar"]).pct_change() * 100
    nvda_ret = fetch_monthly_close(TICKERS["nvda"]).pct_change() * 100
    sp500_ret = fetch_monthly_close(TICKERS["sp500"]).pct_change() * 100
    vix_ret = fetch_monthly_close(TICKERS["vix"]).pct_change() * 100
    oil_ret = fetch_monthly_close(TICKERS["oil"]).pct_change() * 100
    btc_ret = fetch_monthly_close(TICKERS["btc"]).pct_change() * 100

    df = pd.DataFrame({
        "payems_change": payems_change,
        "gold_ret": gold_ret,
        "dollar_ret": dollar_ret,
        "nvda_ret": nvda_ret,
        "sp500_ret": sp500_ret,
        "vix_ret": vix_ret,
        "oil_ret": oil_ret,
        "fedfunds_change": fedfunds_change,
        "btc_ret": btc_ret,
    }).sort_index()

    # 要因Aの基準値: 当該月より前の直近3ヶ月の変化幅平均
    df["payems_baseline"] = df["payems_change"].rolling(window=3).mean().shift(1)
    # シグナルが予測しようとする対象: 翌月のBTC値動き
    df["btc_forward_ret"] = df["btc_ret"].shift(-1)

    return df


def classify(row) -> tuple[str, str] | None:
    if pd.isna(row["payems_change"]) or pd.isna(row["payems_baseline"]):
        return None
    for col in ("gold_ret", "dollar_ret", "nvda_ret", "sp500_ret", "vix_ret", "oil_ret"):
        if pd.isna(row[col]):
            return None
    # fedfunds_changeは変化なし月(0.0)や、シリーズ開始直後でNaNの場合があるため、
    # NaNのみ「投票なし」として扱い判定自体は継続する
    fedfunds_change = row.get("fedfunds_change")

    factor_a = "上振れ" if row["payems_change"] > row["payems_baseline"] else "下振れ"

    risk_votes = 0
    risk_votes += 1 if row["nvda_ret"] > 0 else -1
    risk_votes += 1 if row["sp500_ret"] > 0 else -1
    risk_votes += 1 if row["gold_ret"] < 0 else -1
    risk_votes += 1 if row["dollar_ret"] < 0 else -1
    risk_votes += 1 if row["vix_ret"] < 0 else -1
    risk_votes += 1 if row["oil_ret"] < 0 else -1
    if pd.notna(fedfunds_change):
        if fedfunds_change > 0:
            risk_votes -= 1
        elif fedfunds_change < 0:
            risk_votes += 1
    factor_b = "リスクオン" if risk_votes >= 0 else "リスクオフ"

    return factor_a, factor_b


def summarize(df: pd.DataFrame) -> dict:
    buckets: dict[str, list[float]] = {}
    for _, row in df.iterrows():
        if pd.isna(row["btc_forward_ret"]):
            continue
        classification = classify(row)
        if classification is None:
            continue
        key = f"{classification[0]}×{classification[1]}"
        buckets.setdefault(key, []).append(row["btc_forward_ret"])

    result = {}
    for key, moves in buckets.items():
        up_count = sum(1 for m in moves if m > 0)
        result[key] = {
            "avg_move_pct": round(sum(moves) / len(moves), 2),
            "up_ratio_pct": round(up_count / len(moves) * 100, 1),
            "sample_count": len(moves),
        }
    return result


def run_analysis() -> dict:
    """雇用統計×リスクセンチメントのパターン別・翌月BTC値動き統計をまとめて返す。

    macro_signal_notifier.pyから直接呼び出される想定(毎回最新データで再集計するため、
    ファイルへの保存・手動更新は不要)。
    """
    df = build_monthly_table()
    # fedfunds_changeは「投票なし」を許容するため必須列から除外
    required = [c for c in REQUIRED_COLUMNS if c != "fedfunds_change"]
    valid = df.dropna(subset=required)
    patterns = summarize(df)

    return {
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "data_from": str(valid.index.min()) if not valid.empty else None,
        "data_to": str(valid.index.max()) if not valid.empty else None,
        "patterns": patterns,
    }


def main():
    if not FRED_API_KEY:
        print("[エラー] FRED_API_KEY が設定されていません。")
        return 1

    print("月次マクロデータを取得中(FRED + yfinance)...")
    result = run_analysis()
    if result["data_from"] is None:
        print("[エラー] 有効なデータが取得できませんでした。")
        return 1

    print(f"データが揃っている月: {result['data_from']} 〜 {result['data_to']}")
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {OUTPUT_FILE} を生成しました(参考情報として保存。シグナル判定は毎回再計算します)。")
    for key, stats in result["patterns"].items():
        print(f"  {key}: 翌月平均{stats['avg_move_pct']:+.2f}% / 上昇確率{stats['up_ratio_pct']}% (n={stats['sample_count']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
