"""
journal_add.py

トレードを記録する(エントリー時)。コマンドライン引数で実行する。

使用例:
    python journal_add.py --asset BTC --direction LONG --entry 65000 --size 0.1 --stop 63000 --note "SMAクロス+コンフルエンス"

記録したIDは後で journal_close.py で使用する(トレードを閉じる時)。
"""

import argparse

import db_utils
import risk_utils


def main():
    parser = argparse.ArgumentParser(description="トレードエントリーを記録する")
    parser.add_argument("--asset", required=True, help="銘柄 (例: BTC)")
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"], help="方向")
    parser.add_argument("--entry", required=True, type=float, help="エントリー価格")
    parser.add_argument("--size", required=True, type=float, help="数量")
    parser.add_argument("--stop", type=float, default=None,
                        help="損切り価格(任意。省略すると銘柄の値動きに応じて自動設定)")
    parser.add_argument("--note", type=str, default="", help="メモ(任意。シグナル根拠など)")
    args = parser.parse_args()

    db_utils.init_db()

    # 損切り幅・利確目標はペーパートレードと同じ基準で決める。
    # 省略時に損切り価格を空のままにすると、position_monitor.pyの損切り監視が
    # まるごと素通りしてしまい、記録したのに見張られないポジションになる。
    stop_pct, target_pct = risk_utils.stop_and_target_for(args.asset)
    stop_loss = args.stop
    if stop_loss is None:
        stop_loss = risk_utils.calc_stop_loss_price(args.entry, args.direction, stop_pct)

    entry_id = db_utils.add_journal_entry(
        asset=args.asset, direction=args.direction, entry_price=args.entry,
        size=args.size, stop_loss=stop_loss, note=args.note, target_pct=target_pct,
    )

    print(f"[OK] トレードを記録しました。ID={entry_id}")
    print(f"  {args.asset} {args.direction} @ {args.entry} x {args.size}")
    if args.stop is None:
        print(f"  損切り: {stop_loss:,.4f}(自動設定: 値動きに応じて-{stop_pct:.1f}%)")
    else:
        print(f"  損切り: {stop_loss:,.4f}(指定値)")
    print(f"  利確目安: +{target_pct:.1f}% / 保有期限に達した場合も通知されます")
    print(f"  決済する時は: python journal_close.py --id {entry_id} --exit <決済価格>")


if __name__ == "__main__":
    main()
