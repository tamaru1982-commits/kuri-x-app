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

import html
import re
from datetime import datetime, timedelta
from pathlib import Path

import db_utils

OUTPUT_FILE = Path("docs/index.html")
JST = timedelta(hours=9)

# モバイルでの横スクロールを避けるため短縮表記にする
SOURCE_ABBREV = {
    "crypto_technical": "tech",
    "crypto_dip": "dip",
    "x_post": "x",
    "whale_flow": "whale",
    "macro_pattern": "macro",
    "confluence": "conf",
}

# 資産ごとに文字色を割り当てて識別しやすくする(ダーク背景での視認性・色相の分散を優先した配色)
ASSET_COLORS = {
    "BTC": "#f7931a",
    "DOGE": "#f0c419",
    "SOL": "#4df3c4",
    "EDGE": "#2dd4bf",
    "XRP": "#5ac8fa",
    "SUI": "#4f8cff",
    "ETH": "#9fa8ff",
    "TRIA": "#c084fc",
    "HYPE": "#ff6ec7",
    "AAVE": "#ff5c5c",
    "USDT": "#4dd8ab",
    "USDC": "#5b9bff",
    "市場全体": "#c9c9c9",
}
DEFAULT_ASSET_COLOR = "#e6e6e6"


def to_short_time(iso_timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_timestamp) + JST
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return iso_timestamp


def signal_emoji(direction: str) -> str:
    return "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"


def asset_span(asset: str) -> str:
    color = ASSET_COLORS.get(asset, DEFAULT_ASSET_COLOR)
    return f"<span class='asset' style='color:{color}'>{html.escape(asset)}</span>"


def source_label(source: str) -> str:
    return html.escape(SOURCE_ABBREV.get(source, source[:5]))


def compact_basis(text: str, max_len: int = 22) -> str:
    """改行を除去し、モバイル幅に収まるよう短く切り詰める。"""
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) > max_len:
        flat = flat[:max_len].rstrip() + "…"
    return flat


def compact_price(price) -> str:
    if price is None:
        return ""
    if price >= 1000:
        return f"${price:,.0f}"
    if price >= 1:
        return f"${price:,.2f}"
    return f"${price:.4f}"


def group_by_asset_keep_recency(rows: list) -> list:
    """同じ資産の行をまとめて連続させつつ、グループ自体は直近の出現順を保つ。
    rows は事前に timestamp 降順でソート済みであることを前提とする。"""
    groups: dict[str, list] = {}
    order: list[str] = []
    for r in rows:
        asset = r["asset"]
        if asset not in groups:
            groups[asset] = []
            order.append(asset)
        groups[asset].append(r)

    flattened = []
    for asset in order:
        flattened.extend(groups[asset])
    return flattened


def collect_assets(*row_groups) -> list[str]:
    assets = set()
    for rows in row_groups:
        for r in rows:
            assets.add(r["asset"])
    return sorted(assets)


def render_asset_filter(assets: list[str]) -> str:
    if not assets:
        return ""
    boxes = "".join(
        f"<label class='chip'><input type='checkbox' class='asset-toggle' value='{html.escape(a)}' checked>"
        f"{asset_span(a)}</label>"
        for a in assets
    )
    return f"<div class='filter-bar'>{boxes}</div>"


def render_recent_signals(rows) -> str:
    if not rows:
        return "<p class='muted'>直近のシグナルはありません。</p>"

    latest_30 = sorted(rows, key=lambda x: x["timestamp"], reverse=True)[:30]
    grouped = group_by_asset_keep_recency(latest_30)

    items = []
    for r in grouped:
        full_message = r["message"] or ""
        price_str = compact_price(r["price_at_signal"])
        basis_parts = [p for p in (price_str, compact_basis(full_message)) if p]
        basis = html.escape(" · ".join(basis_parts))
        title_attr = f" title='{html.escape(' '.join(full_message.split()))}'" if full_message else ""
        basis_span = f"<span class='basis'> · {basis}</span>" if basis else ""
        items.append(
            f"<tr data-asset='{html.escape(r['asset'])}'>"
            f"<td>{to_short_time(r['timestamp'])}</td>"
            f"<td>{source_label(r['source'])}</td>"
            f"<td>{asset_span(r['asset'])}</td>"
            f"<td{title_attr}>{signal_emoji(r['direction'])} {r['direction']}{basis_span}</td>"
            f"</tr>"
        )
    return (
        "<table class='filterable'><thead><tr><th>時刻</th><th>ソース</th><th>資産</th>"
        "<th>方向・根拠</th></tr></thead><tbody>" + "".join(items) + "</tbody></table>"
    )


def render_hit_rate(summary) -> str:
    if not summary:
        return "<p class='muted'>まだ的中率を計算できるデータがありません。</p>"

    items = []
    for row in summary:
        items.append(
            f"<tr data-asset='{html.escape(row['asset'])}'>"
            f"<td>{source_label(row['source'])}</td><td>{asset_span(row['asset'])}</td>"
            f"<td>{row['hit_rate_pct']}%</td><td>{row['correct']}/{row['total']}件</td></tr>"
        )
    return (
        "<table class='filterable'><thead><tr><th>ソース</th><th>資産</th><th>的中率</th><th>件数</th></tr></thead>"
        "<tbody>" + "".join(items) + "</tbody></table>"
    )


def render_target_summary(summary) -> str:
    if not summary:
        return "<p class='muted'>まだ目標到達を判定できるデータがありません。</p>"

    items = []
    for row in summary:
        hit_hours = f"{row['avg_hit_hours']}h" if row["avg_hit_hours"] is not None else "-"
        adv_hit = f"{row['avg_adverse_on_hit']}%" if row["avg_adverse_on_hit"] is not None else "-"
        adv_miss = f"{row['avg_adverse_on_miss']}%" if row["avg_adverse_on_miss"] is not None else "-"
        items.append(
            f"<tr data-asset='{html.escape(row['asset'])}'>"
            f"<td>{source_label(row['source'])}</td><td>{asset_span(row['asset'])}</td>"
            f"<td>{signal_emoji(row['direction'])} {row['direction']}</td>"
            f"<td>{row['hit_rate_pct']}% ({row['hit_count']}/{row['total']})</td>"
            f"<td>{hit_hours}</td><td>{adv_hit}</td><td>{adv_miss}</td></tr>"
        )
    target_pct = summary[0]["target_pct"]
    window_days = round(summary[0]["target_window_hours"] / 24, 1) if summary[0]["target_window_hours"] else "-"
    note = f"<p class='muted' style='font-size:0.75rem;'>目標+{target_pct}% / 判定期間{window_days}日 での集計です。</p>"
    return (
        "<table class='filterable'><thead><tr><th>ソース</th><th>資産</th><th>方向</th>"
        "<th>到達率</th><th>平均到達時間</th><th>到達前逆行</th><th>未到達時逆行</th></tr></thead>"
        "<tbody>" + "".join(items) + "</tbody></table>" + note
    )


def render_open_positions(rows) -> str:
    if not rows:
        return "<p class='muted'>保有中のポジションはありません。</p>"

    items = []
    for r in rows:
        stop = f"${r['stop_loss']:,.2f}" if r["stop_loss"] is not None else "-"
        note = html.escape(r["note"] or "-")
        items.append(
            f"<tr data-asset='{html.escape(r['asset'])}'>"
            f"<td>{to_short_time(r['timestamp'])}</td><td>{asset_span(r['asset'])}</td>"
            f"<td>{signal_emoji(r['direction'])} {r['direction']}</td>"
            f"<td>${r['entry_price']:,.2f}</td><td>{r['size']}</td>"
            f"<td>{stop}</td><td>{note}</td></tr>"
        )
    return (
        "<table class='filterable'><thead><tr><th>建玉日時</th><th>資産</th><th>方向</th>"
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


FILTER_SCRIPT = """
<script>
(function () {
  var STORAGE_KEY = "dashboard_hidden_assets";
  var hidden = [];
  try {
    hidden = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch (e) { hidden = []; }

  function applyFilter() {
    var checked = {};
    document.querySelectorAll(".asset-toggle").forEach(function (box) {
      checked[box.value] = box.checked;
    });
    document.querySelectorAll("table.filterable tbody tr").forEach(function (row) {
      var asset = row.getAttribute("data-asset");
      row.style.display = (checked[asset] === false) ? "none" : "";
    });
  }

  document.querySelectorAll(".asset-toggle").forEach(function (box) {
    if (hidden.indexOf(box.value) !== -1) box.checked = false;
    box.addEventListener("change", function () {
      var nowHidden = [];
      document.querySelectorAll(".asset-toggle").forEach(function (b) {
        if (!b.checked) nowHidden.push(b.value);
      });
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(nowHidden)); } catch (e) {}
      applyFilter();
    });
  });

  applyFilter();
})();
</script>
"""


def build_html() -> str:
    recent_signals = db_utils.get_recent_signals(hours=72)
    hit_rate = db_utils.get_hit_rate_summary(hours=24 * 30)
    target_summary = db_utils.get_target_hit_summary(hours=24 * 30)
    open_positions = db_utils.get_open_positions()
    journal_summary = db_utils.get_journal_summary()

    now_str = (datetime.utcnow() + JST).strftime("%m-%d %H:%M")
    asset_options = collect_assets(recent_signals, open_positions, hit_rate)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>シグナル状況ダッシュボード</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 720px; margin: 0 auto; padding: 16px;
    background: #0f1115; color: #e6e6e6;
  }}
  h1 {{ font-size: 1.2rem; margin-bottom: 4px; }}
  .updated {{ color: #8a8f98; font-size: 0.8rem; margin-bottom: 20px; }}
  section {{ margin-bottom: 26px; }}
  h2 {{ font-size: 0.95rem; border-left: 4px solid #4f8cff; padding-left: 8px; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.78rem; table-layout: fixed; }}
  th, td {{ text-align: left; padding: 5px 4px; border-bottom: 1px solid #2a2d34; overflow: hidden; text-overflow: ellipsis; }}
  th {{ color: #8a8f98; font-weight: 500; font-size: 0.72rem; }}
  .asset {{ font-weight: 700; }}
  .basis {{ color: #8a8f98; }}
  .muted {{ color: #8a8f98; font-size: 0.9rem; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .stat {{ background: #1a1d24; border-radius: 8px; padding: 8px 12px; min-width: 100px; }}
  .stat .label {{ display: block; color: #8a8f98; font-size: 0.7rem; }}
  .stat .value {{ display: block; font-size: 1.05rem; font-weight: 600; }}
  .disclaimer {{ color: #8a8f98; font-size: 0.72rem; margin-top: 28px; border-top: 1px solid #2a2d34; padding-top: 12px; }}
  .filter-bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  .chip {{
    display: inline-flex; align-items: center; gap: 4px;
    background: #1a1d24; border-radius: 999px; padding: 4px 10px;
    font-size: 0.78rem; cursor: pointer; user-select: none;
  }}
  .chip input {{ accent-color: #4f8cff; }}
</style>
</head>
<body>
  <h1>🪙 シグナル状況ダッシュボード</h1>
  <div class="updated">最終更新: {now_str} ・ <a href="about.html" style="color:#4f8cff">このアプリについて</a></div>

  {render_asset_filter(asset_options)}

  <section>
    <h2>保有中ポジション</h2>
    {render_open_positions(open_positions)}
  </section>

  <section>
    <h2>トレード成績(決済済み)</h2>
    {render_journal_summary(journal_summary)}
  </section>

  <section>
    <h2>的中率(直近30日)</h2>
    {render_hit_rate(hit_rate)}
  </section>

  <section>
    <h2>目標到達率(直近30日)</h2>
    {render_target_summary(target_summary)}
  </section>

  <section>
    <h2>直近のシグナル(72時間以内・最大30件)</h2>
    {render_recent_signals(recent_signals)}
  </section>

  <div class="disclaimer">
    ※このダッシュボードは記録・分析支援を目的としたものであり、投資助言ではありません。
    自動売買は行っていません。上のチップで表示する資産を絞り込めます(この端末にのみ記憶されます)。
  </div>

  {FILTER_SCRIPT}
</body>
</html>
"""


# ============ about.html(クジラ規模分布の差し込み) ============

ABOUT_TEMPLATE_FILE = Path("about_template.html")
ABOUT_OUTPUT_FILE = Path("docs/about.html")

# whale_signal_notifier.pyのWHALE_TIERSと揃えたランク定義(ダッシュボード側は独立して集計するため複製)
WHALE_TIERS = [
    (100_000_000, "🐳 超大口(メガクジラ)"),
    (50_000_000, "🐋 大口"),
    (10_000_000, "🐬 中口"),
    (0, "🐟 小口"),
]
WHALE_AMOUNT_PATTERN = re.compile(r"\$([\d,]+)\s*$")


def compute_whale_distribution() -> tuple[list[dict], int, int]:
    """whale_flowシグナルのmessageから金額を抜き出し、規模別の件数・割合を集計する。
    戻り値: (ランクごとの内訳, 金額判明件数, 金額不明件数)"""
    conn = db_utils.get_conn()
    rows = conn.execute("SELECT message FROM signals WHERE source = 'whale_flow'").fetchall()
    conn.close()

    counts = {label: 0 for _, label in WHALE_TIERS}
    known_total = 0
    unknown_total = 0

    for row in rows:
        match = WHALE_AMOUNT_PATTERN.search(row["message"] or "")
        if not match:
            unknown_total += 1
            continue
        amount = float(match.group(1).replace(",", ""))
        for threshold, label in WHALE_TIERS:
            if amount >= threshold:
                counts[label] += 1
                break
        known_total += 1

    breakdown = []
    for _, label in WHALE_TIERS:
        pct = round(counts[label] / known_total * 100, 1) if known_total else 0
        breakdown.append({"label": label, "count": counts[label], "pct": pct})

    return breakdown, known_total, unknown_total


def render_whale_distribution() -> str:
    breakdown, known_total, unknown_total = compute_whale_distribution()

    if known_total == 0:
        return "<p class='muted'>まだ金額が判明しているクジラ検知データがありません。</p>"

    bars = []
    for row in breakdown:
        bars.append(
            "<div style='margin-bottom:8px;'>"
            f"<div style='display:flex;justify-content:space-between;font-size:0.85rem;'>"
            f"<span>{html.escape(row['label'])}</span><span>{row['count']}件 ({row['pct']}%)</span></div>"
            f"<div style='background:#262a33;border-radius:4px;height:8px;overflow:hidden;'>"
            f"<div style='background:#4f8cff;height:100%;width:{row['pct']}%;'></div></div>"
            "</div>"
        )

    note = f"<p class='muted' style='font-size:0.75rem;'>合計{known_total}件(金額判明分)を集計"
    if unknown_total:
        note += f"。金額不明{unknown_total}件は除外"
    note += "。あくまで簡易集計で、傾向をざっくり把握するためのものです。</p>"

    return "".join(bars) + note


def generate_about_page():
    if not ABOUT_TEMPLATE_FILE.exists():
        print(f"[警告] {ABOUT_TEMPLATE_FILE} が見つからないため、about.htmlの更新をスキップします。")
        return

    template = ABOUT_TEMPLATE_FILE.read_text(encoding="utf-8")
    rendered = template.replace("<!--WHALE_DISTRIBUTION-->", render_whale_distribution())
    ABOUT_OUTPUT_FILE.parent.mkdir(exist_ok=True)
    ABOUT_OUTPUT_FILE.write_text(rendered, encoding="utf-8")
    print(f"[OK] {ABOUT_OUTPUT_FILE} を生成しました。")


def main():
    db_utils.init_db()
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(build_html(), encoding="utf-8")
    print(f"[OK] {OUTPUT_FILE} を生成しました。")

    generate_about_page()


if __name__ == "__main__":
    main()
