"""
dashboard_generator.py

trading_system.db の内容から、現在の状況(直近シグナル・的中率・保有ポジション・
トレード成績)を1枚の静的HTMLにまとめて docs/index.html に出力する。
GitHub Pagesでホスティングすることで、Discordの通知が来た瞬間だけでなく
いつでも「今どうなっているか」を確認できるようにするためのもの。

dashboard.yml(定期実行)から呼び出され、生成後にdocs/index.htmlがコミットされる。

注意: GitHub Free プランのPrivateリポジトリでは、GitHub PagesのURLは
「知っていれば誰でも閲覧できる」(検索エンジンには載らないが真の非公開ではない)。
"""

from datetime import datetime, timedelta
from pathlib import Path

import db_utils

OUTPUT_FILE = Path("docs/index.html")
JST = timedelta(hours=9)


def to_jst_str(iso_timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_timestamp) + JST
        return dt.strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return iso_timestamp


def signal_emoji(direction: str) -> str:
    return "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"


def render_recent_signals(rows) -> str:
    if not rows:
        return "<p class='muted'>直近のシグナルはありません。</p>"

    items = []
    for r in sorted(rows, key=lambda x: x["timestamp"], reverse=True)[:30]:
        price = f"${r['price_at_signal']:,.2f}" if r["price_at_signal"] is not None else "-"
        items.append(
            f"<tr><td>{to_jst_str(r['timestamp'])}</td>"
            f"<td>{r['source']}</td>"
            f"<td>{r['asset']}</td>"
            f"<td>{signal_emoji(r['direction'])} {r['direction']}</td>"
            f"<td>{price}</td></tr>"
        )
    return (
        "<table><thead><tr><th>時刻</th><th>ソース</th><th>資産</th>"
        "<th>方向</th><th>価格</th></tr></thead><tbody>" + "".join(items) + "</tbody></table>"
    )


def render_hit_rate(summary) -> str:
    if not summary:
        return "<p class='muted'>まだ的中率を計算できるデータがありません。</p>"

    items = []
    for row in summary:
        items.append(
            f"<tr><td>{row['source']}</td><td>{row['asset']}</td>"
            f"<td>{row['hit_rate_pct']}%</td><td>{row['correct']}/{row['total']}件</td></tr>"
        )
    return (
        "<table><thead><tr><th>ソース</th><th>資産</th><th>的中率</th><th>件数</th></tr></thead>"
        "<tbody>" + "".join(items) + "</tbody></table>"
    )


def render_open_positions(rows) -> str:
    if not rows:
        return "<p class='muted'>保有中のポジションはありません。</p>"

    items = []
    for r in rows:
        stop = f"${r['stop_loss']:,.2f}" if r["stop_loss"] is not None else "-"
        items.append(
            f"<tr><td>{to_jst_str(r['timestamp'])}</td><td>{r['asset']}</td>"
            f"<td>{signal_emoji(r['direction'])} {r['direction']}</td>"
            f"<td>${r['entry_price']:,.2f}</td><td>{r['size']}</td>"
            f"<td>{stop}</td><td>{r['note'] or '-'}</td></tr>"
        )
    return (
        "<table><thead><tr><th>建玉日時</th><th>資産</th><th>方向</th>"
        "<th>建値</th><th>数量</th><th>損切り</th><th>メモ</th></tr></thead>"
        "<tbody>" + "".join(items) + "</tbody></table>"
    )


def render_journal_summary(summary) -> str:
    if summary.get("total_trades", 0) == 0:
        return "<p class='muted'>まだ決済済みのトレード記録がありません。</p>"

    return (
        "<div class='stats'>"
        f"<div class='stat'><span class='label'>決済済みトレード数</span><span class='value'>{summary['total_trades']}件</span></div>"
        f"<div class='stat'><span class='label'>勝率</span><span class='value'>{summary['win_rate_pct']}%</span></div>"
        f"<div class='stat'><span class='label'>合計損益</span><span class='value'>{summary['total_pnl']:+.2f}</span></div>"
        f"<div class='stat'><span class='label'>平均利益</span><span class='value'>{summary['avg_win']:+.2f}</span></div>"
        f"<div class='stat'><span class='label'>平均損失</span><span class='value'>{summary['avg_loss']:+.2f}</span></div>"
        "</div>"
    )


def build_html() -> str:
    recent_signals = db_utils.get_recent_signals(hours=72)
    hit_rate = db_utils.get_hit_rate_summary(hours=24 * 30)
    open_positions = db_utils.get_open_positions()
    journal_summary = db_utils.get_journal_summary()

    now_jst = (datetime.utcnow() + JST).strftime("%Y-%m-%d %H:%M JST")

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>シグナル状況ダッシュボード</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 720px; margin: 0 auto; padding: 16px;
    background: #0f1115; color: #e6e6e6;
  }}
  h1 {{ font-size: 1.3rem; margin-bottom: 4px; }}
  .updated {{ color: #8a8f98; font-size: 0.85rem; margin-bottom: 24px; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 1rem; border-left: 4px solid #4f8cff; padding-left: 8px; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a2d34; white-space: nowrap; }}
  th {{ color: #8a8f98; font-weight: 500; }}
  .muted {{ color: #8a8f98; font-size: 0.9rem; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .stat {{ background: #1a1d24; border-radius: 8px; padding: 10px 14px; min-width: 110px; }}
  .stat .label {{ display: block; color: #8a8f98; font-size: 0.75rem; }}
  .stat .value {{ display: block; font-size: 1.1rem; font-weight: 600; }}
  .table-wrap {{ overflow-x: auto; }}
  .disclaimer {{ color: #8a8f98; font-size: 0.75rem; margin-top: 32px; border-top: 1px solid #2a2d34; padding-top: 12px; }}
</style>
</head>
<body>
  <h1>🪙 シグナル状況ダッシュボード</h1>
  <div class="updated">最終更新: {now_jst}</div>

  <section>
    <h2>保有中ポジション</h2>
    <div class="table-wrap">{render_open_positions(open_positions)}</div>
  </section>

  <section>
    <h2>トレード成績(決済済み)</h2>
    {render_journal_summary(journal_summary)}
  </section>

  <section>
    <h2>的中率(直近30日)</h2>
    <div class="table-wrap">{render_hit_rate(hit_rate)}</div>
  </section>

  <section>
    <h2>直近のシグナル(72時間以内・最大30件)</h2>
    <div class="table-wrap">{render_recent_signals(recent_signals)}</div>
  </section>

  <div class="disclaimer">
    ※このダッシュボードは記録・分析支援を目的としたものであり、投資助言ではありません。
    自動売買は行っていません。
  </div>
</body>
</html>
"""


def main():
    db_utils.init_db()
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(build_html(), encoding="utf-8")
    print(f"[OK] {OUTPUT_FILE} を生成しました。")


if __name__ == "__main__":
    main()
