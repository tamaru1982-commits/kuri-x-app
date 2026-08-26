"""
correlation_report.py

追跡している資産(仮想通貨 + ドル指数/金/原油/AI株)の値動きの相関係数を
過去90日分の日次データから計算し、週次でDiscordに通知する。

目的:
「別々の資産にポジションを分けているつもりが、実は同じ方向に賭けていた」
という集中リスクに気づきやすくするための参考情報。
"""

import os
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

LOOKBACK_DAYS = 90

# 仮想通貨(CoinGecko id)
CRYPTO_ASSETS = [
    {"id": "bitcoin", "label": "BTC"},
    {"id": "ethereum", "label": "ETH"},
    {"id": "solana", "label": "SOL"},
    {"id": "ripple", "label": "XRP"},
]

# 伝統資産(yfinanceティッカー)
TRADITIONAL_ASSETS = [
    {"ticker": "DX-Y.NYB", "label": "ドル指数"},
    {"ticker": "GC=F", "label": "金先物"},
    {"ticker": "CL=F", "label": "原油先物"},
    {"ticker": "NVDA", "label": "AI関連株(NVIDIA)"},
]

# 「相関が高い」と警告するしきい値(絶対値)
HIGH_CORRELATION_THRESHOLD = 0.7


def fetch_crypto_series(coin_id: str) -> pd.Series:
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": LOOKBACK_DAYS, "interval": "daily"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    prices = resp.json().get("prices", [])
    df = pd.DataFrame(prices, columns=["ts", "price"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.date
    return df.set_index("date")["price"]


def fetch_traditional_series(ticker: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period=f"{LOOKBACK_DAYS}d")
    if hist.empty:
        return pd.Series(dtype=float)
    hist.index = hist.index.date
    return hist["Close"]


def build_price_matrix() -> pd.DataFrame:
    series_dict = {}

    for asset in CRYPTO_ASSETS:
        try:
            series_dict[asset["label"]] = fetch_crypto_series(asset["id"])
        except Exception as e:
            print(f"[エラー] {asset['label']} 取得失敗: {e}")

    for asset in TRADITIONAL_ASSETS:
        try:
            series_dict[asset["label"]] = fetch_traditional_series(asset["ticker"])
        except Exception as e:
            print(f"[エラー] {asset['label']} 取得失敗: {e}")

    df = pd.DataFrame(series_dict)
    df = df.sort_index().ffill().dropna()
    return df


def build_message(corr: pd.DataFrame) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"**🔗 資産相関レポート ({now_str}, 過去{LOOKBACK_DAYS}日)**", ""]

    labels = list(corr.columns)
    high_corr_pairs = []

    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            value = corr.loc[a, b]
            if pd.notna(value) and abs(value) >= HIGH_CORRELATION_THRESHOLD:
                high_corr_pairs.append((a, b, value))

    if high_corr_pairs:
        lines.append(f"⚠️ **相関が高いペア(|相関|≧{HIGH_CORRELATION_THRESHOLD})**")
        for a, b, value in sorted(high_corr_pairs, key=lambda x: -abs(x[2])):
            direction = "正の相関(同方向に動きやすい)" if value > 0 else "負の相関(逆方向に動きやすい)"
            lines.append(f"　・{a} × {b}: {value:+.2f} ({direction})")
    else:
        lines.append("現時点で強い相関(|相関|≧0.7)のペアはありません。")

    lines.append("")
    lines.append("_※過去90日の日次データに基づく統計的な相関です。将来も同じ関係が続くとは限りません。_")
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
        print("[OK] 相関レポートを送信しました。")


def main():
    price_matrix = build_price_matrix()
    if price_matrix.shape[1] < 2:
        print("十分な資産データが取得できませんでした。")
        return 1

    returns = price_matrix.pct_change().dropna()
    corr = returns.corr()

    message = build_message(corr)
    send_discord_notification(message)
    return 0


if __name__ == "__main__":
    main()
