"""
correlation_report.py

追跡している資産(仮想通貨 + ドル指数/金/原油/AI株)の値動きの相関係数を
過去90日分の日次データから計算し、週次でDiscordに通知する。

目的:
「別々の資産にポジションを分けているつもりが、実は同じ方向に賭けていた」
という集中リスクに気づきやすくするための参考情報。
"""

import os
import sys
import time
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf

import price_utils

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

LOOKBACK_DAYS = 90

# 仮想通貨は監視対象の全銘柄を対象にする。
# 以前はBTC/ETH/SOL/XRPの4銘柄だけを直書きしており、後から追加した6銘柄が
# 相関の集計から漏れていた。「別々の資産に分けたつもりが同じ方向に賭けていた」
# ことに気づくためのレポートなのに、候補の6割が見えていない状態だった。
CRYPTO_ASSETS = [
    {"id": coin_id, "label": symbol}
    for symbol, coin_id in price_utils.SYMBOL_TO_COINGECKO_ID.items()
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
    # 銘柄数が増えたぶんレート制限に当たりやすいため、共通のリトライ処理を使う。
    # ここで落ちるとその銘柄が相関表から静かに消えてしまう。
    resp = price_utils._get_with_retry(url, params)
    prices = resp.json().get("prices", [])
    df = pd.DataFrame(prices, columns=["ts", "price"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.date
    series = df.set_index("date")["price"]
    # CoinGeckoは境界日に複数点を返すことがあるため、同一日は最後の値を採用して重複を除去
    return series[~series.index.duplicated(keep="last")]


def fetch_traditional_series(ticker: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period=f"{LOOKBACK_DAYS}d")
    if hist.empty:
        return pd.Series(dtype=float)
    hist.index = hist.index.date
    series = hist["Close"]
    return series[~series.index.duplicated(keep="last")]


def build_price_matrix() -> pd.DataFrame:
    series_dict = {}

    for asset in CRYPTO_ASSETS:
        try:
            series_dict[asset["label"]] = fetch_crypto_series(asset["id"])
        except Exception as e:
            print(f"[エラー] {asset['label']} 取得失敗: {e}")
        time.sleep(3)  # CoinGecko無料APIのレート制限(429)回避

    for asset in TRADITIONAL_ASSETS:
        try:
            series_dict[asset["label"]] = fetch_traditional_series(asset["ticker"])
        except Exception as e:
            print(f"[エラー] {asset['label']} 取得失敗: {e}")

    df = pd.DataFrame(series_dict)
    # 仮想通貨は24時間365日動くが、伝統資産は平日しか値がつかない。
    # ffill()で週末を金曜終値で埋めると、その日の伝統資産の変化率が0になり
    # (実測で全体の31%)、仮想通貨との相関が実際より弱く出てしまう。
    # 両方が実際に動いた日(=平日)だけで相関を取る。
    df = df.sort_index().dropna()
    return df


def build_message(corr: pd.DataFrame) -> str:
    """レポートを組み立てる。

    監視対象の仮想通貨はほぼ全ペアが高相関になるため(実測で17ペアが0.7超)、
    該当ペアを列挙するだけでは読めないうえ、肝心の「分散できていない」という
    結論が埋もれてしまう。そこで
      1. 仮想通貨同士の平均相関(=分散が効いているかの要約)
      2. 特に連動が強い上位ペア
      3. 仮想通貨と伝統資産の関係(相場全体の地合いを読む手がかり)
    の順に整理して伝える。
    """
    now_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"**🔗 資産相関レポート ({now_str}, 過去{LOOKBACK_DAYS}日)**", ""]

    crypto = [a["label"] for a in CRYPTO_ASSETS if a["label"] in corr.columns]
    trad = [a["label"] for a in TRADITIONAL_ASSETS if a["label"] in corr.columns]

    def pairs_of(xs, ys=None):
        out = []
        if ys is None:
            for i, a in enumerate(xs):
                for b in xs[i + 1:]:
                    v = corr.loc[a, b]
                    if pd.notna(v):
                        out.append((a, b, v))
        else:
            for a in xs:
                for b in ys:
                    v = corr.loc[a, b]
                    if pd.notna(v):
                        out.append((a, b, v))
        return out

    # 1. 仮想通貨同士がどれだけ一体で動いているか
    cc = pairs_of(crypto)
    if cc:
        avg = sum(v for _, _, v in cc) / len(cc)
        high = sum(1 for _, _, v in cc if abs(v) >= HIGH_CORRELATION_THRESHOLD)
        lines.append(f"**仮想通貨同士の平均相関: {avg:+.2f}**({len(cc)}ペア中{high}ペアが0.7超)")
        if avg >= 0.6:
            lines.append("⚠️ 銘柄を分けても値動きはほぼ同じです。**複数銘柄を同時に持っても分散になりません。**")
        lines.append("")
        lines.append("特に連動が強い組み合わせ:")
        for a, b, v in sorted(cc, key=lambda x: -abs(x[2]))[:5]:
            lines.append(f"　・{a} × {b}: {v:+.2f}")
        lines.append("")

    # 2. 仮想通貨と伝統資産(地合いを読む手がかりになる)
    ct = pairs_of(crypto, trad)
    if ct:
        lines.append("**伝統資産との関係(絶対値が大きい順に3件)**")
        for a, b, v in sorted(ct, key=lambda x: -abs(x[2]))[:3]:
            direction = "同方向に動きやすい" if v > 0 else "逆方向に動きやすい"
            lines.append(f"　・{a} × {b}: {v:+.2f}({direction})")
        strong = [p for p in ct if abs(p[2]) >= HIGH_CORRELATION_THRESHOLD]
        if not strong:
            lines.append("　いずれも0.7未満で、仮想通貨は伝統資産とは概ね独立して動いています。")
        lines.append("")

    lines.append(f"_※過去{LOOKBACK_DAYS}日のうち、伝統資産にも値がつく平日の日次データに基づく統計的な相関です。_")
    lines.append("_※将来も同じ関係が続くとは限りません。_")
    return "\n".join(lines)


def send_discord_notification(message: str) -> bool:
    """送信できたかを返す。このスクリプトは通知することだけが目的なので、
    送信に失敗したまま成功扱いにすると通知が止まっていることに気づけない。"""
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return True
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
        return False
    print("[OK] 相関レポートを送信しました。")
    return True


def main():
    price_matrix = build_price_matrix()
    if price_matrix.shape[1] < 2:
        print("十分な資産データが取得できませんでした。")
        return 1

    returns = price_matrix.pct_change().dropna()
    corr = returns.corr()

    message = build_message(corr)
    return 0 if send_discord_notification(message) else 1


if __name__ == "__main__":
    sys.exit(main())
