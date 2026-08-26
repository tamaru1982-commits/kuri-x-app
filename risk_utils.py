"""
risk_utils.py

シグナル通知に「どれくらいの数量で入るべきか」の参考値を添えるための
簡易リスク計算ユーティリティ。

考え方:
- 1回のトレードで失ってよい金額を「口座残高 × リスク許容%」で決める
- 損切りラインまでの値幅から、逆算してポジションサイズを決める
これは一般的な資金管理の考え方(固定比率リスクモデル)であり、
利益を保証するものではありません。
"""

import os

# ============ 設定(必要に応じて環境変数で上書き可能) ============

ACCOUNT_BALANCE_USD = float(os.environ.get("ACCOUNT_BALANCE_USD", "1000"))
RISK_PERCENT_PER_TRADE = float(os.environ.get("RISK_PERCENT_PER_TRADE", "1.0"))  # 口座の何%まで許容するか
DEFAULT_STOP_LOSS_PCT = float(os.environ.get("DEFAULT_STOP_LOSS_PCT", "3.0"))    # 損切りまでの値幅(%)


def suggest_position(price: float, direction: str,
                      balance: float = None, risk_pct: float = None,
                      stop_loss_pct: float = None) -> dict:
    balance = balance if balance is not None else ACCOUNT_BALANCE_USD
    risk_pct = risk_pct if risk_pct is not None else RISK_PERCENT_PER_TRADE
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT

    risk_amount_usd = balance * (risk_pct / 100)

    if direction == "LONG":
        stop_loss_price = price * (1 - stop_loss_pct / 100)
    else:  # SHORT
        stop_loss_price = price * (1 + stop_loss_pct / 100)

    price_risk_per_unit = abs(price - stop_loss_price)
    if price_risk_per_unit == 0:
        return {"error": "stop_loss_pctが0のため計算できません"}

    position_size_units = risk_amount_usd / price_risk_per_unit
    position_value_usd = position_size_units * price

    return {
        "risk_amount_usd": round(risk_amount_usd, 2),
        "stop_loss_price": round(stop_loss_price, 4),
        "position_size_units": round(position_size_units, 6),
        "position_value_usd": round(position_value_usd, 2),
        "balance_used_pct": round(position_value_usd / balance * 100, 1) if balance else None,
    }


def format_risk_line(price: float, direction: str) -> str:
    """Discord通知に1行で添えられる形式の文字列を返す。"""
    result = suggest_position(price, direction)
    if "error" in result:
        return ""

    return (
        f"💰 参考ポジション: {result['position_size_units']}単位"
        f"(約${result['position_value_usd']:,.0f}, 口座の{result['balance_used_pct']}%) "
        f"/ 損切り目安: ${result['stop_loss_price']:,.2f}"
    )
