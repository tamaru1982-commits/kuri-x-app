"""
liquidation_note_add.py

CoinGlassの清算マップ(Liquidation Heatmap)を見て判断した「清算の壁」を記録する。
記録したメモは有効期限内だけダッシュボードに表示される(自動判定には使わない)。

スマホからは GitHub の Actions タブ →「Liquidation Note」→「Run workflow」の
入力フォームで記録する(liquidation_note.yml)。手元のPCで実行すると、
15分おきにDBを更新しているActionsとpushが競合するため、基本はそちらを使う。

使用例:
    python liquidation_note_add.py --asset BTC --price 78600 --side long --note "現在価格のすぐ下、一番濃い帯"
"""

import argparse
import re
import sys

import db_utils
import price_utils

# スマホのフォームからは日本語の選択肢で渡ってくるため、先頭の語で判定する
SIDE_ALIASES = {
    "long": "long",
    "ロング": "long",
    "short": "short",
    "ショート": "short",
}
SIDE_LABELS = {
    "long": "ロング清算の壁(下)",
    "short": "ショート清算の壁(上)",
}


def parse_side(text: str) -> str:
    for prefix, side in SIDE_ALIASES.items():
        if text.strip().lower().startswith(prefix):
            return side
    raise ValueError(f"種類を判定できません: {text!r}(long / short を指定してください)")


def parse_price(text: str) -> float:
    """スマホで入力しやすいよう "$78,600" や "78600ドル" のような表記も受け付ける。"""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        raise ValueError(f"価格を読み取れません: {text!r}")
    price = float(cleaned)
    if price <= 0:
        raise ValueError(f"価格が0以下です: {text!r}")
    return price


def main():
    parser = argparse.ArgumentParser(description="清算マップのメモを記録する")
    parser.add_argument("--asset", required=True, help="銘柄 (例: BTC)")
    parser.add_argument("--price", required=True, help="清算が集中している価格 (例: 78600)")
    parser.add_argument("--side", required=True, help="long(ロング清算の壁) / short(ショート清算の壁)")
    parser.add_argument("--note", default="", help="メモ(任意)")
    parser.add_argument("--hours", default="48", help="ダッシュボードに表示する時間(既定48時間)")
    args = parser.parse_args()

    try:
        asset = args.asset.strip().upper()
        price = parse_price(args.price)
        side = parse_side(args.side)
        valid_hours = float(args.hours.strip() or "48")
    except ValueError as e:
        print(f"[エラー] {e}")
        return 1

    db_utils.init_db()

    # 壁までの距離を後から見られるよう、記録時点の価格も残す(取れなくても記録は続行)
    price_now = None
    try:
        price_now = price_utils.get_current_price(asset)
    except Exception as e:
        print(f"[警告] {asset} の現在価格を取得できませんでした: {e}")

    note_id = db_utils.add_liquidation_note(asset, price, side, args.note.strip(), price_now, valid_hours)

    distance = ""
    if price_now:
        distance = f"(現在 ${price_now:,.2f} から {(price - price_now) / price_now * 100:+.1f}%)"
    print(f"[OK] 清算メモを記録しました(id={note_id}): {asset} ${price:,.2f} "
          f"{SIDE_LABELS[side]}{distance} / 表示期限 {valid_hours:g}時間")
    return 0


if __name__ == "__main__":
    sys.exit(main())
