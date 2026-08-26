"""
journal_close.py

journal_add.py で記録したトレードを決済(クローズ)し、損益(pnl)を自動計算する。

使用例:
    python journal_close.py --id 3 --exit 67000
"""

import argparse

import db_utils


def main():
    parser = argparse.ArgumentParser(description="トレードエントリーを決済する")
    parser.add_argument("--id", required=True, type=int, help="journal_add.pyで表示されたID")
    parser.add_argument("--exit", required=True, type=float, help="決済価格")
    args = parser.parse_args()

    db_utils.init_db()
    try:
        pnl = db_utils.close_journal_entry(args.id, args.exit)
    except ValueError as e:
        print(f"[エラー] {e}")
        return 1

    result = "利益" if pnl > 0 else "損失"
    print(f"[OK] ID={args.id} を決済しました。損益: {pnl:+.2f} ({result})")
    return 0


if __name__ == "__main__":
    main()
