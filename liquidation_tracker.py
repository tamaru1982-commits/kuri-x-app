"""
liquidation_tracker.py

手動で記録した清算メモ(liquidation_notes)について、
「価格が記録した壁に届いたか」「届いた後に押し戻されたか・突き抜けたか」を
価格の推移から事後検証する。清算マップの読み方が実際に役立っているかを
数字で確かめるためのもので、自動シグナルの判定には使わない。

判定:
- 到達: 記録から表示期限(valid_until)までに、価格が壁の価格に届いたか
  (ロング清算の壁=下なら価格が壁以下に、ショート清算の壁=上なら壁以上に)
- 到達後: 届いてから REACTION_WINDOW_HOURS 以内に、
  壁から REACTION_PCT% 以上もとの側へ戻れば「反転」、
  壁の向こうへ REACTION_PCT% 以上進めば「突破」(先に起きた方)、どちらでもなければ「もみ合い」
- 記録時点で既に壁の反対側にいた場合(上下の取り違え等)は「対象外」とする

target_tracker.yml から定期実行される。

注意:
- 価格はCoinGeckoの約1時間ごとの値で、清算マップ(先物)の価格とは少しずれる。
  1時間の間のヒゲは拾えないため、「ギリギリ届いた」ケースは未到達になりやすい。
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import db_utils
import price_utils
from target_tracker import fetch_price_series

REACTION_PCT = float(os.environ.get("LIQ_REACTION_PCT", "1.0"))
REACTION_WINDOW_HOURS = float(os.environ.get("LIQ_REACTION_WINDOW_HOURS", "24"))


def to_utc(iso_timestamp: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromisoformat(iso_timestamp)).tz_localize("UTC")


def evaluate_note(note, series: pd.Series, now: pd.Timestamp) -> dict | None:
    """判定できればその結果を、まだ判定できなければNoneを返す。"""
    start = to_utc(note["timestamp"])
    end = to_utc(note["valid_until"])
    wall = note["price"]
    is_long_wall = note["side"] == "long"

    if series.empty or series.index[0] > start:
        print(f"[スキップ] id={note['id']}: 記録時点の価格データがありません")
        return None

    price_at_record = note["price_at_record"] or float(series[series.index <= start].iloc[-1])
    if (is_long_wall and price_at_record <= wall) or (not is_long_wall and price_at_record >= wall):
        return {"eval_result": "wrong_side", "reached_hours": None, "after_touch": None}

    window = series[(series.index >= start) & (series.index <= end)]
    touched = window[window <= wall] if is_long_wall else window[window >= wall]

    if touched.empty:
        if now < end:
            return None  # まだ表示期限内なので、これから届く可能性がある
        return {"eval_result": "not_reached", "reached_hours": None, "after_touch": None}

    touch_time = touched.index[0]
    reached_hours = round((touch_time - start).total_seconds() / 3600, 1)
    if now < touch_time + timedelta(hours=REACTION_WINDOW_HOURS):
        return None  # 到達後の動きを見る時間がまだ足りない

    after = series[(series.index > touch_time) &
                   (series.index <= touch_time + timedelta(hours=REACTION_WINDOW_HOURS))]
    back_level = wall * (1 + REACTION_PCT / 100) if is_long_wall else wall * (1 - REACTION_PCT / 100)
    through_level = wall * (1 - REACTION_PCT / 100) if is_long_wall else wall * (1 + REACTION_PCT / 100)

    after_touch = "range"
    for price in after:
        went_back = price >= back_level if is_long_wall else price <= back_level
        went_through = price <= through_level if is_long_wall else price >= through_level
        if went_back:
            after_touch = "reversal"
            break
        if went_through:
            after_touch = "breakthrough"
            break

    return {"eval_result": "reached", "reached_hours": reached_hours, "after_touch": after_touch}


def main():
    db_utils.init_db()
    notes = db_utils.get_unevaluated_liquidation_notes()
    if not notes:
        print("検証対象の清算メモはありません。")
        return 0

    now = pd.Timestamp(datetime.now(timezone.utc))
    by_asset: dict[str, list] = {}
    for note in notes:
        by_asset.setdefault(note["asset"], []).append(note)

    for asset, rows in by_asset.items():
        coin_id = price_utils.SYMBOL_TO_COINGECKO_ID.get(asset)
        if not coin_id:
            print(f"[スキップ] {asset}: 価格の対応表に無い銘柄です")
            continue
        try:
            series = fetch_price_series(coin_id)
        except Exception as e:
            print(f"[エラー] {asset} の価格取得に失敗: {e}")
            continue

        for note in rows:
            result = evaluate_note(note, series, now)
            if result is None:
                continue
            db_utils.record_liquidation_eval(note["id"], **result)
            print(f"[検証] id={note['id']} {asset} ${note['price']:,.0f} ({note['side']}): "
                  f"{result['eval_result']} / {result['after_touch']}")

        time.sleep(6)

    return 0


if __name__ == "__main__":
    sys.exit(main())
