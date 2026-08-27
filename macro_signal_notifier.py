"""
macro_signal_notifier.py

macro_pattern_analysis.py の分析(雇用統計×リスクセンチメントのパターン別・翌月BTC
値動き統計)を使い、現在の月次マクロ環境がどのパターンに該当するかを判定し、
十分なサンプル数と偏りがある場合にLONG/SHORTのシグナルとしてDiscordに通知する。

月1回程度の実行を想定(NFP発表後、当月分の数字が出揃ってから)。
db_utilsにsource="macro_pattern"として記録するため、confluence_checker.pyが
他のシグナルとの一致も検知する。

注意:
- 月次×少数パターンでの集計のため、1パターンあたりのサンプル数はせいぜい数十件程度。
  統計的な裏付けは他のシグナル(テクニカル・X投稿・クジラ)と比べて明確に弱い。
  必ず「参考情報」として扱い、これだけで売買判断をしないこと。
- 暗号資産の価格データは2014年頃までしか遡れないため、"2012年まで"の分析はできない。
- 投資助言ではありません。
"""

import os
import sys

import requests
import pandas as pd
import yfinance as yf

import db_utils
import macro_pattern_analysis as mpa

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

MIN_SAMPLE_COUNT = int(os.environ.get("MACRO_MIN_SAMPLE_COUNT", "10"))
SKEW_THRESHOLD_PCT = float(os.environ.get("MACRO_SKEW_THRESHOLD_PCT", "65"))  # 上昇確率がこれ以上/以下でシグナル扱い
COOLDOWN_MINUTES = int(os.environ.get("MACRO_COOLDOWN_MINUTES", str(25 * 24 * 60)))  # 約25日(月1回想定)

SOURCE_NAME = "macro_pattern"


def determine_current_regime() -> tuple[str, str] | None:
    """直近のNFPサプライズ方向 × 直近1ヶ月のリスクセンチメントから現在の環境を判定する。"""
    payems = mpa.fetch_payems_monthly()
    if len(payems) < 4:
        return None
    changes = payems.diff().dropna()
    if len(changes) < 4:
        return None
    latest_change = changes.iloc[-1]
    baseline = changes.iloc[-4:-1].mean()
    factor_a = "上振れ" if latest_change > baseline else "下振れ"

    def latest_monthly_return(ticker: str) -> float | None:
        close = mpa.fetch_monthly_close(ticker)
        if len(close) < 2:
            return None
        return (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100

    gold_ret = latest_monthly_return(mpa.TICKERS["gold"])
    dollar_ret = latest_monthly_return(mpa.TICKERS["dollar"])
    nvda_ret = latest_monthly_return(mpa.TICKERS["nvda"])
    if gold_ret is None or dollar_ret is None or nvda_ret is None:
        return None

    risk_votes = (1 if nvda_ret > 0 else -1) + (1 if gold_ret < 0 else -1) + (1 if dollar_ret < 0 else -1)
    factor_b = "リスクオン" if risk_votes >= 0 else "リスクオフ"

    return factor_a, factor_b


def send_discord_notification(pattern_key: str, stats: dict, signal: str, data_range: str):
    emoji = "🟢" if signal == "LONG" else "🔴"
    message = (
        f"{emoji} **📊 マクロ相関シグナル: {signal} (BTC)**\n"
        f"現在の環境: {pattern_key}\n"
        f"過去の傾向(n={stats['sample_count']}ヶ月, {data_range}): "
        f"翌月平均{stats['avg_move_pct']:+.2f}% / 上昇確率{stats['up_ratio_pct']}%\n\n"
        f"_※雇用統計・金・ドル・AI株(NVDA)の月次パターンに基づく参考情報です。"
        f"サンプル数が少なく統計的根拠は他のシグナルより弱いため、単独での売買判断は避けてください。"
        f"投資助言ではありません。_"
    )

    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
    else:
        print(f"[OK] 通知送信: {signal}")


def main():
    if not FRED_API_KEY:
        print("[エラー] FRED_API_KEY が設定されていません。")
        return 1

    db_utils.init_db()

    print("過去データを再集計中...")
    analysis = mpa.run_analysis()
    if analysis["data_from"] is None:
        print("[エラー] 過去データの取得に失敗しました。")
        return 1
    data_range = f"{analysis['data_from']}〜{analysis['data_to']}"
    print(f"集計対象期間: {data_range}")

    regime = determine_current_regime()
    if regime is None:
        print("現在のマクロ環境を判定するためのデータが不足しています。")
        return 0

    pattern_key = f"{regime[0]}×{regime[1]}"
    stats = analysis["patterns"].get(pattern_key)
    print(f"現在の環境: {pattern_key}")

    if not stats:
        print("該当パターンの過去データがありません。")
        return 0

    print(f"過去統計: 翌月平均{stats['avg_move_pct']:+.2f}% / 上昇確率{stats['up_ratio_pct']}% (n={stats['sample_count']})")

    if stats["sample_count"] < MIN_SAMPLE_COUNT:
        print(f"サンプル数不足(n={stats['sample_count']} < {MIN_SAMPLE_COUNT})のためシグナルなし。")
        return 0

    if stats["up_ratio_pct"] >= SKEW_THRESHOLD_PCT:
        signal = "LONG"
    elif stats["up_ratio_pct"] <= (100 - SKEW_THRESHOLD_PCT):
        signal = "SHORT"
    else:
        print("偏りが閾値未満のためシグナルなし。")
        return 0

    if db_utils.was_recently_notified(SOURCE_NAME, "BTC", signal, COOLDOWN_MINUTES):
        print(f"{signal} だがクールダウン中のためスキップ")
        return 0

    db_utils.log_signal(SOURCE_NAME, "BTC", signal, None, message=pattern_key)
    send_discord_notification(pattern_key, stats, signal, data_range)
    return 0


if __name__ == "__main__":
    sys.exit(main())
