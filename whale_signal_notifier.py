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

TRACKED_ASSETS = {"BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "EDGE", "TRIA", "SUI", "AAVE"}
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD"}

MAX_RESULTS = 10
COOLDOWN_MINUTES = int(os.environ.get("WHALE_COOLDOWN_MINUTES", "60"))

STATE_FILE = Path("whale_signal_state.json")

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

X_API_BASE = "https://api.x.com/2"
SOURCE_NAME = "whale_flow"

# 例: "894 $BTC (69,825,706 USD) transferred from unknown wallet to Coinbase Institutional"
# 資産シンボルは "$BTC" のようにドル記号(cashtag)で表記される
TRANSFER_PATTERN = re.compile(
    r"([\d,.]+)\s+[#$](\w+)\s+\(([\d,.]+)\s*USD\)\s+transferred from\s+(.+?)\s+to\s+(.+)",
    re.IGNORECASE,
)

# 取引所名は "#" タグではなくプレーンテキストで表記され("Coinbase Institutional"等)、
# 一方でDeFiプロトコル("#Aave"等)や"Unknown Whale 1"のような非取引所ラベルにも
# "#"やプレーンテキストが使われるため、"#"の有無ではなく既知の主要取引所名との
# 部分一致で判定する。
KNOWN_EXCHANGES = [
    "binance", "coinbase", "kraken", "bitfinex", "okx", "okex", "bybit",
    "huobi", "htx", "upbit", "bitstamp", "gemini", "crypto.com", "kucoin",
    "gate.io", "mexc", "bithumb", "bittrex", "poloniex", "bitget", "whitebit",
]


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
    """既知の主要取引所名が含まれていれば取引所とみなす。"""
    entity_lower = entity.lower()
    return any(exchange in entity_lower for exchange in KNOWN_EXCHANGES)


# 金額(USD)による簡易ランク付け。Whale Alertの投稿は元々一定規模以上のみが対象なので、
# その中でもさらに大小を区別できるよう閾値を設定している(高い順に判定)。
WHALE_TIERS = [
    (100_000_000, "🐳", "超大口(メガクジラ)"),
    (50_000_000, "🐋", "大口"),
    (10_000_000, "🐬", "中口"),
    (0, "🐟", "小口"),
]


def rank_whale(amount_usd: float | None) -> tuple[str, str]:
    if amount_usd is None:
        return "🐋", "規模不明"
    for threshold, emoji, label in WHALE_TIERS:
        if amount_usd >= threshold:
            return emoji, label
    return "🐟", "小口"


def judge_signal_from_text(text: str) -> dict | None:
    match = TRANSFER_PATTERN.search(text)
    if not match:
        return None

    _, asset, usd_amount_str, source, destination = match.groups()
    asset = asset.upper()

    try:
        amount_usd = float(usd_amount_str.replace(",", ""))
    except ValueError:
        amount_usd = None

    if asset not in TRACKED_ASSETS and asset not in STABLECOINS:
        return None

    into_exchange = is_exchange(destination) and not is_exchange(source)
    out_of_exchange = is_exchange(source) and not is_exchange(destination)

    if into_exchange:
        if asset in STABLECOINS:
            reason = "ステーブルコインが取引所へ入金(買い準備の可能性)"
            signal = "LONG"
        else:
            reason = "取引所へ入金(売り圧力の可能性)"
            signal = "SHORT"
    elif out_of_exchange:
        if asset in STABLECOINS:
            return None  # ステーブルコインの出金は市場シグナルとして扱わない
        reason = "取引所から出金(長期保有の意思表示)"
        signal = "LONG"
    else:
        return None

    return {"asset": asset, "signal": signal, "reason": reason, "amount_usd": amount_usd}


# ============ 通知 ============

def send_discord_notification(tweet_text: str, signal: str, asset: str, reason: str, tweet_id: str, amount_usd: float | None):
    emoji = "🟢" if signal == "LONG" else "🔴"
    rank_emoji, rank_label = rank_whale(amount_usd)
    amount_str = f"約${amount_usd:,.0f}" if amount_usd is not None else "金額不明"
    tweet_url = f"https://x.com/{TARGET_USERNAME}/status/{tweet_id}"
    short_note = "\nℹ️ 現物運用のため「SHORT」は空売りではなく「保有中なら売却/未保有なら買い見送り」の意味です。" if signal == "SHORT" else ""
    message = (
        f"{emoji} **🐋 クジラ検知: {signal} ({asset})**\n"
        f"規模: {rank_emoji} {rank_label}({amount_str})\n"
        f"理由: {reason}\n"
        f"> {tweet_text}\n"
        f"{tweet_url}\n"
        f"{short_note}\n"
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
        result = judge_signal_from_text(text)

        if result:
            asset, signal, reason = result["asset"], result["signal"], result["reason"]
            amount_usd = result.get("amount_usd")
            if db_utils.was_recently_notified(SOURCE_NAME, asset, signal, COOLDOWN_MINUTES):
                print(f"[スキップ] {asset} {signal} はクールダウン中: {text[:30]}...")
            else:
                amount_note = f" ${amount_usd:,.0f}" if amount_usd is not None else ""
                db_utils.log_signal(SOURCE_NAME, asset, signal, None, message=f"{reason}{amount_note}")
                send_discord_notification(text, signal, asset, reason, tweet_id, amount_usd)
        else:
            print(f"[スキップ] 対象外/抽出不可: {text[:200]}")

        newest_id = tweet_id

    if newest_id:
        state["last_seen_id"] = newest_id
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
