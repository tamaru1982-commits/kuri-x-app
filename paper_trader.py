"""
paper_trader.py

各シグナルソース(tech/dip/whale/confluence/macro)からLONGが発生するたびに、
実際には買わず「もし毎回シグナル通りに買っていたら」を検証するための
仮想ポジション(ペーパートレード)を自動でjournalに記録する。

実際にお金を使わずに、どのシグナルソースが実際に儲かる傾向にあるのかを
継続的に検証できるようにするための仕組み。実運用(手動記録)とは
journal.is_paper列で区別され、混同しない。

決済(利確/損切り/テクニカル悪化での自動クローズ)はposition_monitor.pyが
既存の監視ロジックをそのままペーパートレードにも適用する形で行う。

前提:
- 直近処理したsignals.idを状態ファイルに保持し、新規のLONGシグナルのみを処理する
- price_at_signalが記録されていないソース(whale/confluence/macro)は、
  その時点の価格をCoinGeckoから取得する
- 対応表に無い資産(ステーブルコイン・「市場全体」等)はペーパートレード対象外

古いシグナルを建玉しない理由(重要):
建値にはシグナル発生時点の価格を使うため、発生から時間が経ったシグナルを
後から建玉すると「何時間も前の価格で買ったことにして、今の価格と比較する」
ことになり、検証結果が完全に無意味になる。
実際、初回のバックログ処理で26〜52時間前のシグナルをまとめて建玉した結果、
21件すべてが建玉直後に損切りされ(TRIAは16分で-13.5%)、「techは負けるソース」
という誤った結論が出かかった。値動きではなく時間差がそのまま損益として
記録されただけだった。
そのためSTALE_SIGNAL_MAX_MINUTESを超えたシグナルは建玉せずスキップする。

注意:
- 実際の売買ではありません。スリッページ・約定タイミングは考慮していません。
  あくまで「シグナルに機械的に従った場合の参考シミュレーション」です。投資助言ではありません。
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import db_utils
import price_utils
import risk_utils

NOTIONAL_USD = float(os.environ.get("PAPER_NOTIONAL_USD", "100"))  # 1トレードあたりの想定金額

# シグナル発生からこの時間を超えて経過していたら建玉しない。
# 通常運用ではcrypto_signalが00/30分、paper_traderが12/42分なので遅れは最大30分程度。
# それを超えるのはワークフロー停止などの異常時であり、その分を建てると検証が壊れる。
STALE_SIGNAL_MAX_MINUTES = float(os.environ.get("PAPER_STALE_MAX_MINUTES", "60"))

STATE_FILE = Path("paper_trader_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def get_new_long_signals(since_id: int) -> list:
    conn = db_utils.get_conn()
    rows = conn.execute(
        "SELECT * FROM signals WHERE id > ? AND direction = 'LONG' ORDER BY id ASC",
        (since_id,),
    ).fetchall()
    conn.close()
    return rows


def main():
    db_utils.init_db()
    state = load_state()
    last_id = state.get("last_signal_id", 0)

    signals = get_new_long_signals(last_id)
    if not signals:
        print("新規のLONGシグナルはありません。")
        save_state(state)  # state_fileが未作成だとワークフロー側のgit addが失敗するため必ず保存する
        return 0

    # 古すぎるシグナルは建玉しない(建値と現在価格の時間差がそのまま損益として
    # 記録され、検証が無意味になるため)。状態ファイルのidだけは進めて再処理を防ぐ。
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = []
    for sig in signals:
        age_minutes = (now - datetime.fromisoformat(sig["timestamp"])).total_seconds() / 60
        if age_minutes > STALE_SIGNAL_MAX_MINUTES:
            print(f"[スキップ] {sig['source']} {sig['asset']}: "
                  f"発生から{age_minutes:.0f}分経過(上限{STALE_SIGNAL_MAX_MINUTES:.0f}分)のため建玉しません")
            continue
        fresh.append(sig)

    # price_at_signalが無いシグナル用に、必要な銘柄の価格をまとめて1回で取得する
    missing_price_assets = sorted({
        sig["asset"] for sig in fresh
        if sig["price_at_signal"] is None and price_utils.is_supported(sig["asset"])
    })
    fallback_prices = {}
    if missing_price_assets:
        try:
            fallback_prices = price_utils.fetch_prices(missing_price_assets)
        except Exception as e:
            print(f"[エラー] 価格取得に失敗しました: {e}")

    volatility = risk_utils.load_volatility()
    opened = 0
    # スキップした分も含めて再処理しないよう、状態は取得した全シグナルで進める
    max_id = max([last_id] + [sig["id"] for sig in signals])

    for sig in fresh:
        asset = sig["asset"]

        if not price_utils.is_supported(asset):
            print(f"[スキップ] {sig['source']} {asset}: 対応表未登録のため対象外")
            continue

        # 同じ相場を重複して建てない(下記関数のコメント参照)
        if db_utils.has_open_paper_position(asset, sig["source"]):
            print(f"[スキップ] {sig['source']} {asset}: 同ソースで保有中のため建て増ししません")
            continue

        price = sig["price_at_signal"]
        if price is None:
            price = fallback_prices.get(asset)

        if price is None or price <= 0:
            print(f"[スキップ] {sig['source']} {asset}: 価格取得不可")
            continue

        # 損切り幅は銘柄の値動きの荒さに合わせる。一律だと、値動きの荒い銘柄では
        # シグナルの当たり外れに関係なくノイズで損切りされてしまう。
        stop_pct, target_pct = risk_utils.stop_and_target_for(asset, volatility)

        size = NOTIONAL_USD / price
        stop_loss = price * (1 - stop_pct / 100)

        db_utils.add_journal_entry(
            asset=asset, direction="LONG", entry_price=price, size=size, stop_loss=stop_loss,
            note=f"paper:{sig['source']}", is_paper=True, source=sig["source"],
            target_pct=target_pct,
        )
        opened += 1
        print(f"[OK] ペーパー建玉: {sig['source']} {asset} @ ${price:,.4f}"
              f"(想定${NOTIONAL_USD:.0f}分 / 損切り-{stop_pct:.1f}% 利確+{target_pct:.1f}% 日次変動{volatility.get(asset, 0):.1f}%)")

    state["last_signal_id"] = max_id
    save_state(state)
    print(f"{opened}件のペーパートレードを新規記録しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
