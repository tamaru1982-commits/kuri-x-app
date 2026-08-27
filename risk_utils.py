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


def calc_stop_loss_price(price: float, direction: str, stop_loss_pct: float = None) -> float:
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT
    if direction == "LONG":
        return price * (1 - stop_loss_pct / 100)
    else:  # SHORT
        return price * (1 + stop_loss_pct / 100)


def format_risk_line(price: float, direction: str) -> str:
    """Discord通知に1行で添えられる、通貨非依存のリスク管理目安。"""
    stop_price = calc_stop_loss_price(price, direction)
    return (
        f"💰 損切り目安: ${stop_price:,.2f}(現在値から{DEFAULT_STOP_LOSS_PCT:.1f}%) "
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
