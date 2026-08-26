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

# ============ 設定 ============

TARGET_USERNAME = "example_user"  # ← 監視したい人のユーザー名に変更してください

LONG_KEYWORDS = ["買い", "上昇", "強気", "ロング", "bullish", "long"]
SHORT_KEYWORDS = ["売り", "下落", "弱気", "ショート", "bearish", "short"]

# 投稿からどの資産についての言及か判定するための簡易マッピング(通知/DB記録用)
ASSET_KEYWORDS = {
    "BTC": ["btc", "ビットコイン", "bitcoin"],
    "ETH": ["eth", "イーサ", "ethereum"],
    "SOL": ["sol", "ソラナ", "solana"],
    "XRP": ["xrp", "リップル", "ripple"],
}
DEFAULT_ASSET_LABEL = "市場全体"

MAX_RESULTS = 10
COOLDOWN_MINUTES = int(os.environ.get("X_COOLDOWN_MINUTES", "60"))

STATE_FILE = Path("x_signal_state.json")

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

X_API_BASE = "https://api.x.com/2"
SOURCE_NAME = "x_post"


# ============ 状態の読み書き(新着投稿の重複取得防止) ============

def load_last_seen_id() -> str | None:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return data.get("last_seen_id")
        except Exception:
            return None
    return None


def save_last_seen_id(tweet_id: str):
    STATE_FILE.write_text(json.dumps({"last_seen_id": tweet_id}, ensure_ascii=False), encoding="utf-8")


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
    message = (
        f"{emoji} **@{username} の投稿からサイン検出: {signal} ({asset})**\n"
        f"> {tweet_text}\n"
        f"{tweet_url}\n\n"
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

    user_id = get_user_id(TARGET_USERNAME)
    last_seen_id = load_last_seen_id()
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
                db_utils.log_signal(SOURCE_NAME, asset, signal, None, message=text[:200])
                send_discord_notification(TARGET_USERNAME, text, signal, tweet_id, asset)
        elif signal:
            print(f"[情報] 混在シグナルのため通知スキップ: {text[:30]}...")
        else:
            print(f"[スキップ] キーワード該当なし: {text[:30]}...")

        newest_id = tweet_id

    if newest_id:
        save_last_seen_id(newest_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
