"""
position_monitor.py

journal(status='open')に記録されているポジションを監視し、以下のいずれかを
検知したら通知する。

- 損切りライン(記録時に指定したstop_loss)を割り込んだ
- 利確目標(既定+5%)に到達した
- 保有銘柄でcrypto_technicalの新しいSHORT(売り)サインが出た
- 保有期限(既定72時間)に達した

journal_add.pyで記録した**実ポジション**の場合は、Discordでアラートするのみ
(売るかどうかは人が判断する)。

paper_trader.pyが自動記録した**ペーパートレード**(is_paper=1)の場合は、
人の判断を待たず自動決済する。これにより、各シグナルソースが実際に儲かる
傾向にあるかを実弾なしで継続検証できる。

ペーパートレードの決済は「今この瞬間の価格」ではなく、建玉してからの値動きの
経路(約1時間ごとの価格)をたどり、最初に起きた条件の時刻・価格で決済する。
GitHub Actionsの定期実行は実際には4時間前後おきにしか動かず、現在価格だけを
見ていると、その間に利確目標に届いて戻った動きを取りこぼしたり、損切りが
実際より何時間も遅れて大きな損失として記録されたりするため。

crypto_signal_notifier.py実行後を追いかける形で、30分おきの実行を想定。

前提:
- journalはLONG(買い)エントリーの記録を想定(SHORTは現物運用のため「売却/見送り」
  の意味で使っており、実際にSHORTポジションを記録することは基本的にない)
- 実ポジションの同一種類の通知は既定12時間のクールダウンで再通知を抑制
  (ペーパートレードは決済後status='closed'になるため、再チェック対象から自然に外れる)

注意:
- 価格変動の方向・比率のみに基づく単純な判定です。投資助言ではありません。
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import db_utils
import price_utils
from target_tracker import fetch_price_series

TARGET_PROFIT_PCT = float(os.environ.get("POSITION_TARGET_PROFIT_PCT", "5.0"))
ALERT_COOLDOWN_HOURS = float(os.environ.get("POSITION_ALERT_COOLDOWN_HOURS", "12"))

# 保有期限。「1〜3日以内に+5%を狙う」という戦略である以上、期限内にどちらにも
# 到達しなかった時点で手仕舞うところまでが戦略のはず。
# これが無いと、利確にも損切りにも当たらないポジションが永久に保有され続け、
# 決済済みデータがいつまでも溜まらないまま「大きく動いたトレードだけ」が
# 集計対象になって成績が偏る(実測で、条件次第では7割以上が未決着のまま残る)。
MAX_HOLD_HOURS = float(os.environ.get("POSITION_MAX_HOLD_HOURS", "72"))
TECH_SIGNAL_LOOKBACK_HOURS = float(os.environ.get("POSITION_TECH_LOOKBACK_HOURS", "2"))

# ペーパートレードの決済価格に往復分の想定手数料を反映し、成績が実態より
# 良く見えすぎないようにする(片道0.15%を想定。国内取引所の現物手数料の目安)。
PAPER_FEE_PCT = float(os.environ.get("PAPER_FEE_PCT", "0.15"))

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = Path("position_monitor_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def has_recent_short_signal(asset: str, hours: float) -> bool:
    conn = db_utils.get_conn()
    row = conn.execute(
        "SELECT 1 FROM signals WHERE source = 'crypto_technical' AND asset = ? AND direction = 'SHORT' "
        "AND datetime(timestamp) >= datetime('now', ?) LIMIT 1",
        (asset, f"-{hours} hours"),
    ).fetchone()
    conn.close()
    return row is not None


def was_recently_alerted(state: dict, journal_id: int, alert_type: str, cooldown_hours: float) -> bool:
    last = state.get(f"{journal_id}:{alert_type}")
    if not last:
        return False
    elapsed_hours = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last)).total_seconds() / 3600
    return elapsed_hours < cooldown_hours


def mark_alerted(state: dict, journal_id: int, alert_type: str):
    state[f"{journal_id}:{alert_type}"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def build_alert_message(row, current_price: float, pnl_pct: float, alert_types: list[str],
                        target_profit_pct: float = TARGET_PROFIT_PCT) -> str:
    lines = [f"**📌 保有ポジション通知: {row['asset']}**", ""]
    lines.append(f"建値 ${row['entry_price']:,.4f} → 現在値 ${current_price:,.4f}({pnl_pct:+.2f}%)")
    if row["stop_loss"] is not None:
        lines.append(f"損切りライン: ${row['stop_loss']:,.4f}")
    lines.append("")

    if "take_profit" in alert_types:
        lines.append(f"🟢 **利確目安(+{target_profit_pct:.1f}%)に到達しています。**")
    if "stop_loss" in alert_types:
        lines.append("🔴 **損切りラインを割り込んでいます。**")
    if "tech_short" in alert_types:
        lines.append("⚠️ この銘柄でテクニカル的に売り(SHORT)サインも出ています。")
    if "time_exit" in alert_types:
        lines.append(f"⏱️ 保有期限({MAX_HOLD_HOURS:.0f}時間)に達しました。手仕舞いのご検討を。")

    if row["note"]:
        lines.append(f"\nメモ: {row['note']}")

    lines.append("")
    lines.append(f"_※記録したポジション(journal id={row['id']})に対する参考通知です。投資助言ではありません。_")
    return "\n".join(lines)


def build_paper_close_message(row, market_price: float, fee_adjusted_pnl_pct: float, alert_types: list[str],
                              target_profit_pct: float = TARGET_PROFIT_PCT) -> str:
    """ペーパートレード(is_paper=1)は人の判断を待たず自動決済するため、
    「ご検討ください」ではなく「決済しました」の報告として通知する。
    損益は往復手数料(既定0.3%)を差し引いた実態に近い値を表示する。"""
    lines = [f"**📝 ペーパートレード決済: {row['asset']}**", ""]
    lines.append(f"建値 ${row['entry_price']:,.4f} → 決済価格 ${market_price:,.4f}")
    lines.append(f"手数料(往復{PAPER_FEE_PCT * 2:.2f}%)考慮後 損益: {fee_adjusted_pnl_pct:+.2f}%")
    if row["source"]:
        lines.append(f"シグナル元: {row['source']}")
    lines.append("")

    if "take_profit" in alert_types:
        lines.append(f"🟢 利確目安(+{target_profit_pct:.1f}%)到達で決済")
    if "stop_loss" in alert_types:
        lines.append("🔴 損切りライン到達で決済")
    if "tech_short" in alert_types:
        lines.append("⚠️ テクニカル売りサイン発生で決済")
    if "time_exit" in alert_types:
        lines.append(f"⏱️ 保有期限({MAX_HOLD_HOURS:.0f}時間)到達で決済")

    lines.append("")
    lines.append(
        f"_※実際の売買ではない自動シミュレーションです(journal id={row['id']})。"
        f"シグナルソース別の実力を検証するための記録です。投資助言ではありません。_"
    )
    return "\n".join(lines)


def get_tech_short_signals_since(asset: str, since_iso: str) -> list:
    conn = db_utils.get_conn()
    rows = conn.execute(
        "SELECT timestamp, price_at_signal FROM signals WHERE source = 'crypto_technical' AND asset = ? "
        "AND direction = 'SHORT' AND timestamp > ? ORDER BY timestamp",
        (asset, since_iso),
    ).fetchall()
    conn.close()
    return rows


def find_paper_exit(row, series: pd.Series, now: pd.Timestamp, current_price: float | None,
                    short_signals: list) -> tuple[pd.Timestamp, float, str] | None:
    """建玉後の値動きをたどり、最初に起きた決済条件の(時刻, 価格, 種類)を返す。
    まだどの条件も起きていなければNone。LONGのみ対応(ペーパートレードはLONGのみ建てる)。

    損切り・利確はその価格で約定したものとみなす(逆指値・指値を置いていた想定)。"""
    entry_time = pd.Timestamp(datetime.fromisoformat(row["timestamp"])).tz_localize("UTC")
    entry = row["entry_price"]
    target_pct = row["target_pct"] if row["target_pct"] is not None else TARGET_PROFIT_PCT
    target_price = entry * (1 + target_pct / 100)
    stop_price = row["stop_loss"]
    deadline = entry_time + pd.Timedelta(hours=MAX_HOLD_HOURS)

    path = series[series.index > entry_time]
    if current_price is not None:
        # 時系列の最後の点から今までの動きも拾うため、現在価格を末尾に足す
        path = pd.concat([path, pd.Series([current_price], index=[now])])

    events = []
    for ts, price in path.items():
        if ts > deadline:
            break
        if stop_price is not None and price <= stop_price:
            events.append((ts, stop_price, "stop_loss"))
            break
        if price >= target_price:
            events.append((ts, target_price, "take_profit"))
            break

    for sig in short_signals:
        sig_time = pd.Timestamp(datetime.fromisoformat(sig["timestamp"])).tz_localize("UTC")
        if sig_time <= deadline and sig["price_at_signal"]:
            events.append((sig_time, sig["price_at_signal"], "tech_short"))
            break

    if not events and now >= deadline:
        # 期限の時点の価格で手仕舞う(期限直後の最初の価格。無ければ現在価格)
        after_deadline = path[path.index >= deadline]
        price_at_deadline = float(after_deadline.iloc[0]) if not after_deadline.empty else current_price
        if price_at_deadline is not None:
            events.append((deadline, price_at_deadline, "time_exit"))

    return min(events, key=lambda e: e[0]) if events else None


def settle_paper_positions(positions: list, prices: dict):
    now = pd.Timestamp(datetime.now(timezone.utc))
    by_asset: dict[str, list] = {}
    for row in positions:
        by_asset.setdefault(row["asset"], []).append(row)

    for asset, rows in by_asset.items():
        coin_id = price_utils.SYMBOL_TO_COINGECKO_ID.get(asset)
        if not coin_id:
            print(f"[スキップ] {asset}: 価格の対応表に無い銘柄です")
            continue
        try:
            series = fetch_price_series(coin_id)
        except Exception as e:
            # 現在価格だけで決済すると経路判定と基準が混ざるため、この回は見送って次回に回す
            print(f"[エラー] {asset} の値動き取得に失敗したため、ペーパー決済判定を次回に回します: {e}")
            continue

        for row in rows:
            short_signals = get_tech_short_signals_since(asset, row["timestamp"])
            exit_event = find_paper_exit(row, series, now, prices.get(asset), short_signals)
            if exit_event is None:
                current = prices.get(asset)
                pnl_note = f"含み損益{(current - row['entry_price']) / row['entry_price'] * 100:+.2f}%" if current else "価格不明"
                print(f"{asset} (id={row['id']}, ペーパー): {pnl_note} (決済条件なし)")
                continue

            exit_time, exit_price, exit_type = exit_event
            # 記録するexit_priceには往復手数料を織り込み、成績が実態より良く見えないようにする
            fee_adjusted_exit = exit_price * (1 - (2 * PAPER_FEE_PCT / 100))
            fee_adjusted_pnl_pct = (fee_adjusted_exit - row["entry_price"]) / row["entry_price"] * 100
            exit_iso = exit_time.tz_convert("UTC").tz_localize(None).isoformat()

            db_utils.close_journal_entry(row["id"], fee_adjusted_exit, exit_timestamp=exit_iso)
            target_pct = row["target_pct"] if row["target_pct"] is not None else TARGET_PROFIT_PCT
            send_discord_notification(build_paper_close_message(row, exit_price, fee_adjusted_pnl_pct, [exit_type], target_pct))
            print(f"[ペーパー決済] {asset} (id={row['id']}, source={row['source']}): {exit_type} @ {exit_iso} "
                  f"/ 手数料考慮後損益{fee_adjusted_pnl_pct:+.2f}%")


def send_discord_notification(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL が設定されていないため、コンソールに出力のみ行います。")
        print(message)
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知に失敗しました: {resp.status_code} {resp.text}")
    else:
        print("[OK] ポジション監視通知を送信しました。")


def main():
    db_utils.init_db()
    state = load_state()

    positions = db_utils.get_open_positions()
    if not positions:
        print("保有中のポジションはありません。")
        save_state(state)  # state_fileが未作成だとワークフロー側のgit addが失敗するため必ず保存する
        return 0

    # 保有銘柄の価格は1回のリクエストでまとめて取得する(同じ銘柄の複数ポジションでも
    # 呼び出しは1回で済み、CoinGeckoのレート制限を受けにくい)
    try:
        prices = price_utils.fetch_prices(sorted({row["asset"] for row in positions}))
    except Exception as e:
        print(f"[エラー] 価格取得に失敗しました: {e}")
        save_state(state)
        return 0

    settle_paper_positions([row for row in positions if row["is_paper"]], prices)

    for row in positions:
        if row["is_paper"]:
            continue
        asset = row["asset"]
        current_price = prices.get(asset)
        if current_price is None:
            print(f"[スキップ] {asset}: 価格取得不可(対応表未登録の可能性)")
            continue

        if row["direction"] == "LONG":
            pnl_pct = (current_price - row["entry_price"]) / row["entry_price"] * 100
        else:
            pnl_pct = (row["entry_price"] - current_price) / row["entry_price"] * 100

        alert_types = []

        # 利確目標はポジションごとに持つ(銘柄の値動きの荒さに応じて損切り幅を変えており、
        # 損切りと利確の比率を保つため利確目標も銘柄ごとに変わる)。
        # 記録が無い古いポジションや手動記録分は従来どおりの既定値を使う。
        target_profit_pct = row["target_pct"] if row["target_pct"] is not None else TARGET_PROFIT_PCT

        if pnl_pct >= target_profit_pct and not was_recently_alerted(state, row["id"], "take_profit", ALERT_COOLDOWN_HOURS):
            alert_types.append("take_profit")

        if row["stop_loss"] is not None:
            stop_breached = (
                current_price <= row["stop_loss"] if row["direction"] == "LONG"
                else current_price >= row["stop_loss"]
            )
            if stop_breached and not was_recently_alerted(state, row["id"], "stop_loss", ALERT_COOLDOWN_HOURS):
                alert_types.append("stop_loss")

        if row["direction"] == "LONG" and has_recent_short_signal(asset, TECH_SIGNAL_LOOKBACK_HOURS):
            if not was_recently_alerted(state, row["id"], "tech_short", ALERT_COOLDOWN_HOURS):
                alert_types.append("tech_short")

        held_hours = (datetime.now(timezone.utc).replace(tzinfo=None)
                      - datetime.fromisoformat(row["timestamp"])).total_seconds() / 3600
        if held_hours >= MAX_HOLD_HOURS and not was_recently_alerted(state, row["id"], "time_exit", ALERT_COOLDOWN_HOURS):
            alert_types.append("time_exit")

        if alert_types:
            for alert_type in alert_types:
                mark_alerted(state, row["id"], alert_type)

            send_discord_notification(build_alert_message(row, current_price, pnl_pct, alert_types, target_profit_pct))
            print(f"[通知] {asset} (id={row['id']}): {','.join(alert_types)} / 含み損益{pnl_pct:+.2f}%")
        else:
            print(f"{asset} (id={row['id']}, 実): 含み損益{pnl_pct:+.2f}% (通知条件なし)")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
