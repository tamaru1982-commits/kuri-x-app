"""
db_utils.py

全モード(仮想通貨テクニカル/X投稿/経済指標/コンフルエンス)共通で使う
SQLiteデータベースのヘルパー関数群。

テーブル構成:
- signals: 発生したシグナルの履歴と、後追いでの的中/不的中の記録
- journal: 手動で記録するトレード日誌
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path("trading_system.db")

# 「的中」と見なすために必要な最低変動幅(%)。
# 往復手数料(片道0.15%×2=0.3%)を超えて動いて初めて実際には利益になるため、
# それ未満の微動を「的中」に数えると的中率が実態より甘く出る。
HIT_THRESHOLD_PCT = float(os.environ.get("HIT_THRESHOLD_PCT", "0.3"))

# 判定ルールを変更した日時(UTC)。これより前のシグナルは集計から除外する。
#
# 2026-08-29に、techの判定を「トレンド継続中も出す」から「日足のクロスのみ」へ、
# 目標到達率の基準を一律+5%から銘柄ごとの値へ変更した。
# 基準の違うシグナルを1つの的中率にまとめると、その数字が何を意味するのか
# 説明できなくなるため、集計対象を新ルール以降に限定する。
# (シグナル履歴自体は残すので、必要ならこの値を変えて過去分も見られる)
STATS_SINCE = os.environ.get("STATS_SINCE", "2026-08-29 13:30:00")


def utc_now_iso() -> str:
    """DBに保存する時刻文字列(UTC・タイムゾーン表記なし)。
    datetime.utcnow()は非推奨のため置き換えたが、保存フォーマットは
    既存データとの比較互換のため従来どおりtzinfoなしのISO形式を維持する。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,           -- 'crypto_technical' | 'x_post' | 'confluence'
            asset TEXT NOT NULL,            -- 例: BTC, ETH
            direction TEXT NOT NULL,        -- 'LONG' | 'SHORT'
            price_at_signal REAL,
            message TEXT,
            price_1h REAL,
            price_24h REAL,
            outcome_1h TEXT,                -- 'correct' | 'incorrect' | NULL(未検証)
            outcome_24h TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            asset TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size REAL NOT NULL,
            stop_loss REAL,
            note TEXT,
            exit_price REAL,
            exit_timestamp TEXT,
            pnl REAL,
            status TEXT DEFAULT 'open'      -- 'open' | 'closed'
        )
    """)

    # target_tracker.py用: 目標%到達までの経路追跡カラム(後から追加したため既存DBにはALTERで補う)
    existing_signal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
    target_columns = {
        "target_pct": "REAL",
        "target_window_hours": "REAL",
        "target_hit": "TEXT",           # 'yes' | 'no' | NULL(未確定)
        "target_hit_hours": "REAL",     # 到達までの時間(未到達ならNULL)
        "max_adverse_pct": "REAL",      # 到達前(または判定終了まで)の最大逆行幅(%)
    }
    for col, col_type in target_columns.items():
        if col not in existing_signal_columns:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")

    # paper_trader.py用: 実トレードと自動シミュレーション(ペーパートレード)を区別するカラム
    existing_journal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(journal)").fetchall()}
    journal_columns = {
        "is_paper": "INTEGER DEFAULT 0",  # 1ならpaper_trader.pyが自動記録した仮想トレード
        "source": "TEXT",                 # ペーパートレードの場合、発生元シグナルソース
        # 利確目標(%)。銘柄の値動きの荒さに応じて損切り幅を変えるようにしたため、
        # 利確目標も一律ではなくポジションごとに保持する必要がある
        "target_pct": "REAL",
    }
    for col, col_type in journal_columns.items():
        if col not in existing_journal_columns:
            conn.execute(f"ALTER TABLE journal ADD COLUMN {col} {col_type}")

    conn.commit()
    conn.close()


# ============ signals テーブル ============

def log_signal(source: str, asset: str, direction: str, price: float | None, message: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO signals (timestamp, source, asset, direction, price_at_signal, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), source, asset, direction, price, message),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def was_recently_notified(source: str, asset: str, direction: str, cooldown_minutes: int) -> bool:
    """指定した資産・方向・ソースについて、直近cooldown_minutes以内に通知済みかどうか。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT timestamp FROM signals WHERE source = ? AND asset = ? AND direction = ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (source, asset, direction),
    ).fetchone()
    conn.close()

    if not row:
        return False

    last_time = datetime.fromisoformat(row["timestamp"])
    elapsed_minutes = (datetime.now(timezone.utc).replace(tzinfo=None) - last_time).total_seconds() / 60
    return elapsed_minutes < cooldown_minutes


def get_recent_signals(hours: int, sources: list[str] | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    query = "SELECT * FROM signals WHERE datetime(timestamp) >= datetime('now', ?)"
    params: list = [f"-{hours} hours"]
    if sources:
        placeholders = ",".join("?" for _ in sources)
        query += f" AND source IN ({placeholders})"
        params.extend(sources)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_pending_outcome_signals(min_age_hours: float, max_age_hours: float, outcome_field: str) -> list[sqlite3.Row]:
    """outcome_field('outcome_1h'または'outcome_24h')がまだ未記録で、
    発生からmin_age_hours〜max_age_hours経過したシグナルを返す。"""
    conn = get_conn()
    query = f"""
        SELECT * FROM signals
        WHERE {outcome_field} IS NULL
        AND price_at_signal IS NOT NULL
        AND datetime(timestamp) <= datetime('now', ?)
        AND datetime(timestamp) >= datetime('now', ?)
    """
    rows = conn.execute(query, [f"-{min_age_hours} hours", f"-{max_age_hours} hours"]).fetchall()
    conn.close()
    return rows


def record_outcome(signal_id: int, field_price: str, field_outcome: str, price_now: float):
    conn = get_conn()
    row = conn.execute("SELECT price_at_signal, direction FROM signals WHERE id = ?", (signal_id,)).fetchone()
    price_then = row["price_at_signal"]
    direction = row["direction"]

    # 単なる方向の一致ではなく、往復手数料を超えて動いたかどうかで判定する。
    # (+0.001%でも「的中」にすると、実際には手数料負けする動きまで的中に数えてしまう)
    move_pct = (price_now - price_then) / price_then * 100
    if direction == "SHORT":
        move_pct = -move_pct
    outcome = "correct" if move_pct >= HIT_THRESHOLD_PCT else "incorrect"

    conn.execute(
        f"UPDATE signals SET {field_price} = ?, {field_outcome} = ? WHERE id = ?",
        (price_now, outcome, signal_id),
    )
    conn.commit()
    conn.close()


def get_hit_rate_summary(hours: int = 24 * 30) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT source, asset,
               COUNT(*) as total,
               SUM(CASE WHEN outcome_24h = 'correct' THEN 1 ELSE 0 END) as correct_count
        FROM signals
        WHERE outcome_24h IS NOT NULL
        AND datetime(timestamp) >= datetime('now', '-{hours} hours')
        AND datetime(timestamp) >= datetime('{STATS_SINCE}')
        GROUP BY source, asset
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        rate = (r["correct_count"] / r["total"] * 100) if r["total"] else 0
        result.append({
            "source": r["source"], "asset": r["asset"],
            "total": r["total"], "correct": r["correct_count"], "hit_rate_pct": round(rate, 1),
        })
    return result


def get_hit_rate_by_source(hours: int = 24 * 30) -> list[dict]:
    """的中率をソース単位で集計する(銘柄をまたいで合算)。
    source×assetで分けるとサンプルが細切れになり統計的に意味を持たないため、
    「どのソースが効いているか」を見るにはこちらを使う。"""
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT source,
               COUNT(*) as total,
               SUM(CASE WHEN outcome_24h = 'correct' THEN 1 ELSE 0 END) as correct_count
        FROM signals
        WHERE outcome_24h IS NOT NULL
        AND datetime(timestamp) >= datetime('now', '-{hours} hours')
        AND datetime(timestamp) >= datetime('{STATS_SINCE}')
        GROUP BY source
        ORDER BY total DESC
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        rate = (r["correct_count"] / r["total"] * 100) if r["total"] else 0
        result.append({
            "source": r["source"], "total": r["total"],
            "correct": r["correct_count"], "hit_rate_pct": round(rate, 1),
        })
    return result


def get_target_hit_summary(hours: int = 24 * 30) -> list[dict]:
    """target_tracker.pyが記録した「目標%到達」の集計。到達率・平均到達時間・
    到達/未到達それぞれの平均最大逆行幅(含み損)をsource・asset・方向別に返す。"""
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT source, asset, direction,
               COUNT(*) as total,
               SUM(CASE WHEN target_hit = 'yes' THEN 1 ELSE 0 END) as hit_count,
               AVG(CASE WHEN target_hit = 'yes' THEN target_hit_hours END) as avg_hit_hours,
               AVG(CASE WHEN target_hit = 'yes' THEN max_adverse_pct END) as avg_adverse_on_hit,
               AVG(CASE WHEN target_hit = 'no' THEN max_adverse_pct END) as avg_adverse_on_miss,
               MAX(target_pct) as target_pct,
               MAX(target_window_hours) as target_window_hours
        FROM signals
        WHERE target_hit IS NOT NULL
        AND datetime(timestamp) >= datetime('now', '-{hours} hours')
        AND datetime(timestamp) >= datetime('{STATS_SINCE}')
        GROUP BY source, asset, direction
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        rate = (r["hit_count"] / r["total"] * 100) if r["total"] else 0
        result.append({
            "source": r["source"], "asset": r["asset"], "direction": r["direction"],
            "total": r["total"], "hit_count": r["hit_count"], "hit_rate_pct": round(rate, 1),
            "avg_hit_hours": round(r["avg_hit_hours"], 1) if r["avg_hit_hours"] is not None else None,
            "avg_adverse_on_hit": round(r["avg_adverse_on_hit"], 2) if r["avg_adverse_on_hit"] is not None else None,
            "avg_adverse_on_miss": round(r["avg_adverse_on_miss"], 2) if r["avg_adverse_on_miss"] is not None else None,
            "target_pct": r["target_pct"], "target_window_hours": r["target_window_hours"],
        })
    return result


# ============ journal テーブル ============

def add_journal_entry(asset: str, direction: str, entry_price: float, size: float,
                       stop_loss: float | None, note: str,
                       is_paper: bool = False, source: str | None = None,
                       target_pct: float | None = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO journal (timestamp, asset, direction, entry_price, size, stop_loss, note, is_paper, source, target_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), asset, direction, entry_price, size, stop_loss, note,
         1 if is_paper else 0, source, target_pct),
    )
    conn.commit()
    entry_id = cur.lastrowid
    conn.close()
    return entry_id


def close_journal_entry(entry_id: int, exit_price: float):
    conn = get_conn()
    row = conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"journal id {entry_id} が見つかりません")

    if row["direction"] == "LONG":
        pnl = (exit_price - row["entry_price"]) * row["size"]
    else:
        pnl = (row["entry_price"] - exit_price) * row["size"]

    conn.execute(
        "UPDATE journal SET exit_price = ?, exit_timestamp = ?, pnl = ?, status = 'closed' WHERE id = ?",
        (exit_price, utc_now_iso(), pnl, entry_id),
    )
    conn.commit()
    conn.close()
    return pnl


def get_journal_summary(is_paper: bool | None = None) -> dict:
    conn = get_conn()
    if is_paper is None:
        rows = conn.execute("SELECT * FROM journal WHERE status = 'closed'").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM journal WHERE status = 'closed' AND is_paper = ?", (1 if is_paper else 0,)
        ).fetchall()
    conn.close()

    if not rows:
        return {"total_trades": 0}

    total_pnl = sum(r["pnl"] for r in rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]

    return {
        "total_trades": len(rows),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(r["pnl"] for r in losses) / len(losses), 2) if losses else 0,
    }


def get_open_positions(is_paper: bool | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if is_paper is None:
        rows = conn.execute("SELECT * FROM journal WHERE status = 'open' ORDER BY timestamp DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM journal WHERE status = 'open' AND is_paper = ? ORDER BY timestamp DESC",
            (1 if is_paper else 0,),
        ).fetchall()
    conn.close()
    return rows


def get_paper_performance_by_source(hours: int = 24 * 30) -> list[dict]:
    """ペーパートレードの、発生元シグナルソース別の成績(決済済みのみ)。"""
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT source, asset,
               COUNT(*) as total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
               SUM(pnl) as total_pnl,
               AVG(pnl) as avg_pnl
        FROM journal
        WHERE is_paper = 1 AND status = 'closed'
        AND datetime(exit_timestamp) >= datetime('now', '-{hours} hours')
        GROUP BY source, asset
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        rate = (r["win_count"] / r["total"] * 100) if r["total"] else 0
        result.append({
            "source": r["source"], "asset": r["asset"], "total": r["total"],
            "win_count": r["win_count"], "win_rate_pct": round(rate, 1),
            "total_pnl": round(r["total_pnl"], 2), "avg_pnl": round(r["avg_pnl"], 2),
        })
    return result


def get_paper_performance_window(start_hours_ago: int, end_hours_ago: int) -> list[dict]:
    """ペーパートレードのソース別成績を、指定した期間の窓
    (今からstart_hours_ago〜end_hours_ago時間前の間に決済されたもの)で集計する。
    週次レポートで「今週」「先週」を比較するために資産をまたいでsource単位に集計する。"""
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT source,
               COUNT(*) as total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
               SUM(pnl) as total_pnl,
               AVG(pnl) as avg_pnl
        FROM journal
        WHERE is_paper = 1 AND status = 'closed'
        AND datetime(exit_timestamp) >= datetime('now', '-{start_hours_ago} hours')
        AND datetime(exit_timestamp) < datetime('now', '-{end_hours_ago} hours')
        GROUP BY source
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        rate = (r["win_count"] / r["total"] * 100) if r["total"] else 0
        result.append({
            "source": r["source"], "total": r["total"],
            "win_count": r["win_count"], "win_rate_pct": round(rate, 1),
            "total_pnl": round(r["total_pnl"], 2), "avg_pnl": round(r["avg_pnl"], 2),
        })
    return result


def get_paper_trades_chronological(source: str) -> list[dict]:
    """指定ソースの決済済みペーパートレードを、決済日時の古い順で返す。
    複利シミュレーション(週次レポート)で、各トレードの騰落率を順番に適用するために使う。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT entry_price, exit_price, direction, exit_timestamp FROM journal "
        "WHERE is_paper = 1 AND status = 'closed' AND source = ? "
        "ORDER BY datetime(exit_timestamp) ASC",
        (source,),
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        if r["direction"] == "LONG":
            pnl_pct = (r["exit_price"] - r["entry_price"]) / r["entry_price"] * 100
        else:
            pnl_pct = (r["entry_price"] - r["exit_price"]) / r["entry_price"] * 100
        result.append({"pnl_pct": pnl_pct, "exit_timestamp": r["exit_timestamp"]})
    return result


def has_open_paper_position(asset: str, source: str) -> bool:
    """同じ資産・同じシグナルソースで、まだ決済していないペーパートレードがあるか。

    crypto_technicalの判定は「クロスした瞬間」ではなく「トレンドが続いている状態」で
    真になるため、同じ相場が何度もシグナル化される(実測でETH LONGが5回)。
    これを毎回建玉すると、実質1つの相場への賭けを複数トレードとして数えてしまい、
    勝率も複利シミュレーションも実態から乖離する。既に保有中なら建て増さない。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM journal WHERE is_paper = 1 AND status = 'open' AND asset = ? AND source = ? LIMIT 1",
        (asset, source),
    ).fetchone()
    conn.close()
    return row is not None


def get_paper_sources() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT source FROM journal WHERE is_paper = 1 AND status = 'closed' AND source IS NOT NULL"
    ).fetchall()
    conn.close()
    return [r["source"] for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"[OK] {DB_FILE} を初期化しました。")
