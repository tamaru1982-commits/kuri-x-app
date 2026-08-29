"""
economic_calendar_reminder.py

FRED(米連邦準備制度、無料API)から複数の経済指標の発表日を確認し、
発表前日にDiscordへリマインド通知する汎用版。
NFP(雇用統計)に加え、CPI(消費者物価指数)、PCE(個人消費支出)に対応。

NFPについては nfp_pattern_analysis.py で作成した過去パターン(nfp_patterns.json)も
併せて通知する。CPI/PCEは現時点では発表日のリマインドのみ(過去パターン分析は未対応)。

FOMC(政策金利発表)はFRED上で汎用的な発表日データセットがないため、
FOMC_MEETING_DATES に手動で日程を追加する方式にしている。
最新の日程は https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm を参照。
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

import requests

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
FRED_BASE = "https://api.stlouisfed.org/fred"

REMIND_DAYS_BEFORE = 1
STATE_FILE = Path("economic_calendar_state.json")
NFP_PATTERNS_FILE = Path("nfp_patterns.json")

# FREDのrelease_id一覧
RELEASES = [
    {"release_id": 50, "label": "米雇用統計(NFP)", "key": "nfp"},
    {"release_id": 10, "label": "消費者物価指数(CPI)", "key": "cpi"},
    {"release_id": 54, "label": "個人消費支出(PCE)", "key": "pce"},
]

# FOMC会合日程(手動更新。公式: federalreserve.gov/monetarypolicy/fomccalendars.htm)
FOMC_MEETING_DATES = [
    # 例: "2026-09-16", "2026-09-17"(会合最終日=政策発表日)を追加してください
]


def get_next_release_date(release_id: int) -> str | None:
    url = f"{FRED_BASE}/release/dates"
    params = {
        "release_id": release_id, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "asc", "include_release_dates_with_no_data": "true",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    today = datetime.now().strftime("%Y-%m-%d")
    future = sorted(d["date"] for d in data.get("release_dates", []) if d["date"] >= today)
    return future[0] if future else None


def get_next_fomc_date() -> str | None:
    today = datetime.now().strftime("%Y-%m-%d")
    future = sorted(d for d in FOMC_MEETING_DATES if d >= today)
    return future[0] if future else None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def build_nfp_pattern_section() -> str:
    if not NFP_PATTERNS_FILE.exists():
        return ""

    data = json.loads(NFP_PATTERNS_FILE.read_text(encoding="utf-8"))
    patterns = data.get("patterns", {})
    lines = [f"\n_過去{data.get('lookback_releases', '?')}回分の発表を集計(参考情報)_\n"]

    for bucket, assets in patterns.items():
        lines.append(f"**【{bucket}】だった場合の過去の傾向**")
        for label, stats in assets.items():
            avg = stats["avg_move_pct"]
            up = stats["up_ratio_pct"]
            n = stats["sample_count"]
            direction = "上昇" if avg > 0 else "下落" if avg < 0 else "横ばい"
            lines.append(f"　・{label}: 平均{avg:+.2f}%({direction}方向) / 上昇回数 {up:.0f}% (n={n})")
        lines.append("")

    return "\n".join(lines)


def send_discord_notification(message: str) -> bool:
    """送信できたかを返す。このスクリプトは通知することだけが目的なので、
    送信に失敗したまま成功扱いにすると通知が止まっていることに気づけない。"""
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return True
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
        return False
    print("[OK] リマインド通知を送信しました。")
    return True


def check_and_notify(key: str, label: str, release_date: str | None, state: dict):
    if not release_date:
        return

    today = datetime.now().date()
    release_dt = datetime.strptime(release_date, "%Y-%m-%d").date()
    days_until = (release_dt - today).days

    if days_until != REMIND_DAYS_BEFORE:
        return

    if state.get(key) == release_date:
        print(f"[{label}] {release_date} は通知済みです。")
        return

    lines = [f"**📅 {label} リマインド**", "", f"次回発表日: **{release_date}**"]
    if key == "nfp":
        lines.append(build_nfp_pattern_section())
    lines.append("_※過去の統計的傾向・日程情報であり、結果や値動きを予測・保証するものではありません。_")

    send_discord_notification("\n".join(lines))
    state[key] = release_date


def main():
    if not FRED_API_KEY:
        print("[エラー] FRED_API_KEY が設定されていません。")
        return 1

    state = load_state()

    ok = True
    for release in RELEASES:
        try:
            next_date = get_next_release_date(release["release_id"])
            print(f"{release['label']}: 次回 {next_date}")
            ok &= check_and_notify(release["key"], release["label"], next_date, state)
        except Exception as e:
            print(f"[エラー] {release['label']} の取得に失敗: {e}")
            ok = False

    if FOMC_MEETING_DATES:
        next_fomc = get_next_fomc_date()
        print(f"FOMC: 次回 {next_fomc}")
        ok &= check_and_notify("fomc", "FOMC政策金利発表", next_fomc, state)
    else:
        print("[情報] FOMC_MEETING_DATES が未設定のため、FOMCリマインドはスキップされました。")

    # 送信できた分の記録は残したうえで、失敗があればワークフローに知らせる
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
