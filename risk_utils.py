"""
risk_utils.py

シグナル通知に「損切りライン」と「リスク管理の目安」を添えるための
簡易ユーティリティ。

以前はドル建て口座残高($1000固定)を前提にポジションサイズ・金額を計算していたが、
以下2点の問題があり、比率ベースのシンプルな注意書きに変更した:

1. 実際の運用資金は円建て(1万円スタート)だが、価格データはCoinGecko由来のドル建てで
   通貨が一致しておらず、「口座の◯%」という金額計算が実態と合っていなかった
2. 複数銘柄で同時にLONG/SHORTシグナルが出た場合、銘柄ごとに独立して
   「口座の33%」のような表示がされ、合計すると口座資金を超える矛盾があった

方針:
- 損切りラインは価格に対する%なので通貨に依存せず計算・表示できる(そのまま維持)
- 実際の数量・金額換算は、口座資金(円)と実際のレートに応じて各自で計算してもらう
- 複数銘柄が同時にシグナルを出した場合は、金額を按分計算する代わりに
  「同時に追うのは基本1銘柄まで」という運用ルールを注意書きとして通知に添える
"""

import os

RISK_PERCENT_PER_TRADE = float(os.environ.get("RISK_PERCENT_PER_TRADE", "1.0"))  # 1トレードの損失許容(口座資金の%)
DEFAULT_STOP_LOSS_PCT = float(os.environ.get("DEFAULT_STOP_LOSS_PCT", "3.0"))    # 損切りまでの値幅(%)

# 利確目標は損切り幅に対する倍率で決める(既定 5%利確 / 3%損切り = 1.67倍)。
# 倍率を固定しておけば、銘柄ごとに損切り幅を変えても損益分岐となる勝率は一定に保たれる。
TARGET_TO_STOP_RATIO = float(os.environ.get("TARGET_TO_STOP_RATIO", "1.6667"))

# ボラティリティに応じた損切り幅の設定。
#
# 一律3%の損切りは、日次変動が2.2%(BTC)から15.8%(EDGE)まで7倍も違う銘柄群に対して
# 適切ではなかった。BTCにとって3%は意味のある下落だが、EDGEにとっては数時間で
# 当たり前に動く範囲であり、シグナルの当たり外れに関係なく損切りされてしまう。
# 実測(90日・全銘柄)でも、72時間以内に決着がつかなかった割合はBTCが65%、EDGEが1%と
# 極端に偏っており、値動きの荒い銘柄ほど検証が成立していなかった。
#
# 日次変動の STOP_VOLATILITY_MULTIPLIER 倍を損切り幅とし、極端な値は上下限で抑える。
#
# なお、ランダムなタイミングで入った場合のバックテストでは、幅を広げるほど
# 相場のドリフトを長く浴びるため成績は改善しない(90日・下落局面での実測で
# 一律-3%が-0.26%/回、変動連動1.5倍が-0.46%/回)。それでも変動連動にしているのは、
# 一律の幅では値動きの荒い銘柄がノイズだけで損切りされ、シグナルの当たり外れを
# 測れなくなるため。今は検証段階であり、勝ち負けより「シグナルの良し悪しが
# 結果に反映されること」を優先する。倍率は控えめの1.2倍に留めている。
STOP_VOLATILITY_MULTIPLIER = float(os.environ.get("STOP_VOLATILITY_MULTIPLIER", "1.2"))
MIN_STOP_LOSS_PCT = float(os.environ.get("MIN_STOP_LOSS_PCT", "2.0"))
MAX_STOP_LOSS_PCT = float(os.environ.get("MAX_STOP_LOSS_PCT", "8.0"))


def stop_loss_pct_for_volatility(daily_volatility_pct: float | None) -> float:
    """日次変動率(%)から、その銘柄に見合った損切り幅(%)を求める。
    変動率が不明な場合は従来どおりの既定値を使う。"""
    if daily_volatility_pct is None or daily_volatility_pct <= 0:
        return DEFAULT_STOP_LOSS_PCT
    raw = daily_volatility_pct * STOP_VOLATILITY_MULTIPLIER
    return max(MIN_STOP_LOSS_PCT, min(MAX_STOP_LOSS_PCT, raw))


def target_pct_for_stop(stop_loss_pct: float) -> float:
    """損切り幅に対応する利確目標(%)。比率を固定して損益分岐の勝率を一定に保つ。"""
    return stop_loss_pct * TARGET_TO_STOP_RATIO


def calc_stop_loss_price(price: float, direction: str, stop_loss_pct: float = None) -> float:
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT
    if direction == "LONG":
        return price * (1 - stop_loss_pct / 100)
    else:  # SHORT
        return price * (1 + stop_loss_pct / 100)


def format_risk_line(price: float, direction: str, stop_loss_pct: float | None = None) -> str:
    """Discord通知に1行で添えられる、通貨非依存のリスク管理目安。

    stop_loss_pctを渡すと、その銘柄の値動きの荒さに応じた幅で表示する。
    ペーパートレード側と同じ基準を出さないと、通知を見て売買する人と
    システムの検証結果が別々のルールで動くことになってしまうため。"""
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT
    stop_price = calc_stop_loss_price(price, direction, stop_loss_pct)
    target_pct = target_pct_for_stop(stop_loss_pct)
    return (
        f"💰 損切り目安: ${stop_price:,.2f}(現在値から{stop_loss_pct:.1f}%) "
        f"／ 利確目安 +{target_pct:.1f}% "
        f"／ 1トレードの損失許容は口座資金の{RISK_PERCENT_PER_TRADE:.1f}%までを目安に"
    )


def format_multi_signal_caution(active_signal_count: int) -> str:
    """同時に複数銘柄でLONG/SHORTシグナルが出た場合の注意書き。0または1件なら空文字列。"""
    if active_signal_count <= 1:
        return ""
    return (
        f"⚠️ 今回は{active_signal_count}銘柄で同時にシグナルが出ています。"
        f"資金を分散しすぎないよう、同時に追うのは基本1銘柄までを推奨します。"
    )
