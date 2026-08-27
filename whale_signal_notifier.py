"""
whale_signal_notifier.py

Whale Alert(大口の暗号資産移動を自動検知して投稿するXアカウント @whale_alert)の
投稿を監視し、「取引所への入金/出金」を正規表現で抽出してLONG/SHORTのサインとして
Discordに通知するスクリプト。x_signal_notifier.pyと同じ仕組み(X API + 状態ファイル)を
流用している。

Whale Alert公式APIは有料だが、そのXアカウントの投稿を読むだけならX APIの
通常料金(従量課金)で済むため、ほぼ追加コスト無しで実現できる。

判定ロジック(簡易的な経験則):
- BTC/ETH等の主要資産が取引所へ入金された → 売り圧力の可能性(SHORT)
- ステーブルコイン(USDT/USDC等)が取引所へ入金された → 買い準備の可能性(LONG)
- 資産が取引所から出金された → 長期保有の意思表示(LONG)
  (ステーブルコインの出金は市場シグナルとしての意味が薄いため対象外)

前提:
- X Developer Portal でBearer Tokenを取得済みであること(x_signal_notifierと共用可)

注意:
- Whale Alertの投稿文フォーマットの単純なパターンマッチによる判定です。
  投稿フォーマットが変更されると抽出できなくなる可能性があります。
- 大口移動は取引所の内部振替(コールドウォレット間移動など)である場合もあり、
  必ずしも売買意図を意味しません。投資助言ではありません。
"""

import os
import re
import sys
import json
from pathlib import Path

import requests

import db_utils

# ============ 設定 ============

TARGET_USERNAME = "whale_alert"

TRACKED_ASSETS = {"BTC", "ETH", "SOL", "XRP"}
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD"}

MAX_RESULTS = 10
COOLDOWN_MINUTES = int(os.environ.get("WHALE_COOLDOWN_MINUTES", "60"))

STATE_FILE = Path("whale_signal_state.json")

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

X_API_BASE = "https://api.x.com/2"
SOURCE_NAME = "whale_flow"

# 例: "1,000 #BTC (65,000,000 USD) transferred from #Binance to unknown wallet"
TRANSFER_PATTERN = re.compile(
    r"([\d,.]+)\s+#(\w+)\s+\(([\d,.]+)\s*USD\)\s+transferred from\s+(.+?)\s+to\s+(.+)",
    re.IGNORECASE,
)


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


# ============ X API呼び出し(x_signal_notifier.pyと同一パターン) ============

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
        "exclude": "replies",
    }
    if since_id:
        params["since_id"] = since_id

    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


# ============ シグナル判定 ============

def is_exchange(entity: str) -> bool:
    """'#Binance'のようにハッシュタグ付きの既知エンティティなら取引所とみなす。"""
    return "#" in entity


def judge_signal_from_text(text: str) -> dict | None:
    match = TRANSFER_PATTERN.search(text)
    if not match:
        return None

    _, asset, _, source, destination = match.groups()
    asset = asset.upper()

    if asset not in TRACKED_ASSETS and asset not in STABLECOINS:
        return None

    into_exchange = is_exchange(destination) and not is_exchange(source)
    out_of_exchange = is_exchange(source) and not is_exchange(destination)

    if into_exchange:
        if asset in STABLECOINS:
            return {"asset": asset, "signal": "LONG", "reason": "ステーブルコインが取引所へ入金(買い準備の可能性)"}
        else:
            return {"asset": asset, "signal": "SHORT", "reason": "取引所へ入金(売り圧力の可能性)"}
    elif out_of_exchange:
        if asset in STABLECOINS:
            return None  # ステーブルコインの出金は市場シグナルとして扱わない
        return {"asset": asset, "signal": "LONG", "reason": "取引所から出金(長期保有の意思表示)"}

    return None


# ============ 通知 ============

def send_discord_notification(tweet_text: str, signal: str, asset: str, reason: str, tweet_id: str):
    emoji = "🟢" if signal == "LONG" else "🔴"
    tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet_id}"
    message = (
        f"{emoji} **🐋 クジラ検知: {signal} ({asset})**\n"
        f"理由: {reason}\n"
        f"> {tweet_text}\n"
        f"{tweet_url}\n\n"
        f"_※大口移動の単純なパターンマッチによる簡易判定です。投資助言ではありません。_"
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
        result = judge_signal_from_text(text)

        if result:
            asset, signal, reason = result["asset"], result["signal"], result["reason"]
            if db_utils.was_recently_notified(SOURCE_NAME, asset, signal, COOLDOWN_MINUTES):
                print(f"[スキップ] {asset} {signal} はクールダウン中: {text[:30]}...")
            else:
                db_utils.log_signal(SOURCE_NAME, asset, signal, None, message=text[:200])
                send_discord_notification(text, signal, asset, reason, tweet_id)
        else:
            print(f"[スキップ] 対象外/抽出不可: {text[:30]}...")

        newest_id = tweet_id

    if newest_id:
        save_last_seen_id(newest_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
