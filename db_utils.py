"""
db_utils.py

全モード(仮想通貨テクニカル/X投稿/経済指標/コンフルエンス)共通で使う
SQLiteデータベースのヘルパー関数群。

テーブル構成:
- signals: 発生したシグナルの履歴と、後追いでの的中/不的中の記録
- journal: 手動で記録するトレード日誌
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_FILE = Path("trading_system.db")


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
    conn.commit()
    conn.close()


# ============ signals テーブル ============

def log_signal(source: str, asset: str, direction: str, price: float | None, message: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO signals (timestamp, source, asset, direction, price_at_signal, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), source, asset, direction, price, message),
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
    elapsed_minutes = (datetime.utcnow() - last_time).total_seconds() / 60
    return elapsed_minutes < cooldown_minutes


def get_recent_signals(hours: int, sources: list[str] | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    query = "SELECT * FROM signals WHERE timestamp >= datetime('now', ?)"
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
        AND timestamp <= datetime('now', ?)
        AND timestamp >= datetime('now', ?)
    """
    rows = conn.execute(query, [f"-{min_age_hours} hours", f"-{max_age_hours} hours"]).fetchall()
    conn.close()
    return rows


def record_outcome(signal_id: int, field_price: str, field_outcome: str, price_now: float):
    conn = get_conn()
    row = conn.execute("SELECT price_at_signal, direction FROM signals WHERE id = ?", (signal_id,)).fetchone()
    price_then = row["price_at_signal"]
    direction = row["direction"]

    if direction == "LONG":
        outcome = "correct" if price_now > price_then else "incorrect"
    else:  # SHORT
        outcome = "correct" if price_now < price_then else "incorrect"

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
        AND timestamp >= datetime('now', '-{hours} hours')
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


# ============ journal テーブル ============

def add_journal_entry(asset: str, direction: str, entry_price: float, size: float,
                       stop_loss: float | None, note: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO journal (timestamp, asset, direction, entry_price, size, stop_loss, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), asset, direction, entry_price, size, stop_loss, note),
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
        (exit_price, datetime.utcnow().isoformat(), pnl, entry_id),
    )
    conn.commit()
    conn.close()
    return pnl


def get_journal_summary() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM journal WHERE status = 'closed'").fetchall()
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


def get_open_positions() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM journal WHERE status = 'open' ORDER BY timestamp DESC").fetchall()
    conn.close()
    return rows


if __name__ == "__main__":
    init_db()
    print(f"[OK] {DB_FILE} を初期化しました。")
