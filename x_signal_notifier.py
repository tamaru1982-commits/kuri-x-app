"""
x_signal_notifier.py

X(旧Twitter) API を使って特定アカウントの最新投稿を取得し、
投稿文に含まれるキーワードから LONG / SHORT のサインを判定して
Discordに通知するスクリプト。

Phase2更新:
- シグナルをSQLite(db_utils)に記録(コンフルエンス判定・履歴確認用)
- クールダウン(同一資産・同一方向は一定時間以内は再通知しない)を追加

前提:
- X Developer Portal でアプリを作成し、Bearer Token を取得済みであること
- 公式APIのみを使用し、スクレイピングは行わない(規約違反リスク回避のため)

注意:
- キーワードマッチによる単純な判定です。投稿の文脈やニュアンスまでは判定できません。
- 投資助言ではありません。実際の売買判断は自己責任で行ってください。
"""

import os
import sys
import json
from pathlib import Path

import requests

import db_utils
import price_utils

# ============ 設定 ============

TARGET_USERNAME = "neko_btc_trader"

LONG_KEYWORDS = ["買い", "上昇", "強気", "ロング", "bullish", "long"]
SHORT_KEYWORDS = ["売り", "下落", "弱気", "ショート", "bearish", "short"]

# 投稿からどの資産についての言及か判定するための簡易マッピング(通知/DB記録用)
ASSET_KEYWORDS = {
    "BTC": ["btc", "ビットコイン", "bitcoin"],
    "ETH": ["eth", "イーサ", "ethereum"],
    "SOL": ["sol", "ソラナ", "solana"],
    "XRP": ["xrp", "リップル", "ripple"],
    # HYPE/EDGE/TRIA/SUIは一般英単語と衝突しやすいため、bareな短縮形は避けて誤検知を抑える
    "HYPE": ["hyperliquid", "$hype", "ハイパーリキッド"],
    "DOGE": ["doge", "dogecoin", "ドージコイン"],
    "EDGE": ["edgex", "$edge", "エッジエックス"],
    "TRIA": ["$tria", "triacoin", "トライア"],
    "SUI": ["$sui", "sui network", "スイネットワーク"],
    "AAVE": ["aave", "アーベ"],
}
DEFAULT_ASSET_LABEL = "市場全体"

MAX_RESULTS = 10
COOLDOWN_MINUTES = int(os.environ.get("X_COOLDOWN_MINUTES", "60"))

STATE_FILE = Path("x_signal_state.json")

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

X_API_BASE = "https://api.x.com/2"
SOURCE_NAME = "x_post"


# ============ 状態の読み書き(新着投稿の重複取得防止 + ユーザーIDキャッシュ) ============

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ============ X API呼び出し ============

def get_user_id(username: str) -> str:
    url = f"{X_API_BASE}/users/by/username/{username}"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def get_recent_tweets(user_id: str, since_id: str | None) -> list[dict]:
    url = f"{X_API_BASE}/users/{user_id}/tweets"
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    params = {
        "max_results": MAX_RESULTS,
        "tweet.fields": "created_at,text",
        "exclude": "retweets,replies",
    }
    if since_id:
        params["since_id"] = since_id

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


# ============ シグナル判定 ============

def judge_signal_from_text(text: str) -> str | None:
    lowered = text.lower()
    is_long = any(kw.lower() in lowered for kw in LONG_KEYWORDS)
    is_short = any(kw.lower() in lowered for kw in SHORT_KEYWORDS)

    if is_long and not is_short:
        return "LONG"
    elif is_short and not is_long:
        return "SHORT"
    elif is_long and is_short:
        return "混在(要確認)"
    else:
        return None


def detect_asset(text: str) -> str:
    lowered = text.lower()
    for symbol, keywords in ASSET_KEYWORDS.items():
        if any(kw.lower() in lowered for kw in keywords):
            return symbol
    return DEFAULT_ASSET_LABEL


# ============ 通知 ============

def send_discord_notification(username: str, tweet_text: str, signal: str, tweet_id: str, asset: str):
    if signal == "LONG":
        emoji = "🟢"
    elif signal == "SHORT":
        emoji = "🔴"
    else:
        emoji = "⚠️"

    tweet_url = f"https://x.com/{username}/status/{tweet_id}"
    short_note = "\nℹ️ 現物運用のため「SHORT」は空売りではなく「保有中なら売却/未保有なら買い見送り」の意味です。" if signal == "SHORT" else ""
    message = (
        f"{emoji} **@{username} の投稿からサイン検出: {signal} ({asset})**\n"
        f"> {tweet_text}\n"
        f"{tweet_url}\n"
        f"{short_note}\n"
        f"_※キーワードマッチによる簡易判定です。投資助言ではありません。_"
    )

    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL 未設定のためコンソール出力のみ:")
        print(message)
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知失敗: {resp.status_code} {resp.text}")
    else:
        print(f"[OK] 通知送信: {signal} ({asset})")


# ============ メイン処理 ============

def main():
    if not X_BEARER_TOKEN:
        print("[エラー] X_BEARER_TOKEN が設定されていません。")
        return 1

    db_utils.init_db()

    state = load_state()
    if state.get("user_id") and state.get("username") == TARGET_USERNAME:
        user_id = state["user_id"]
    else:
        user_id = get_user_id(TARGET_USERNAME)
        state["user_id"] = user_id
        state["username"] = TARGET_USERNAME
        save_state(state)  # 後続処理が失敗しても無駄な再取得をしないよう先に保存

    last_seen_id = state.get("last_seen_id")
    tweets = get_recent_tweets(user_id, last_seen_id)

    if not tweets:
        print("新着投稿はありません。")
        return 0

    tweets = list(reversed(tweets))
    newest_id = last_seen_id

    for tweet in tweets:
        text = tweet["text"]
        tweet_id = tweet["id"]
        signal = judge_signal_from_text(text)

        if signal in ("LONG", "SHORT"):
            asset = detect_asset(text)
            if db_utils.was_recently_notified(SOURCE_NAME, asset, signal, COOLDOWN_MINUTES):
                print(f"[スキップ] {asset} {signal} はクールダウン中: {text[:30]}...")
            else:
                # 発生時点の価格を記録しないと後から的中率・目標到達率を検証できない
                try:
                    price_now = price_utils.get_current_price(asset)
                except Exception as e:
                    print(f"[警告] {asset} の価格取得に失敗しました(検証対象外として記録します): {e}")
                    price_now = None
                db_utils.log_signal(SOURCE_NAME, asset, signal, price_now, message=text[:200])
                send_discord_notification(TARGET_USERNAME, text, signal, tweet_id, asset)
        elif signal:
            print(f"[情報] 混在シグナルのため通知スキップ: {text[:30]}...")
        else:
            print(f"[スキップ] キーワード該当なし: {text[:30]}...")

        newest_id = tweet_id

    if newest_id:
        state["last_seen_id"] = newest_id
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
