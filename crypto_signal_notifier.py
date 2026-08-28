"""
crypto_signal_notifier.py

無料・APIキー不要の CoinGecko API から仮想通貨価格を取得し、
シンプルなテクニカル指標（SMAクロス + RSI）で LONG / SHORT / 様子見 を判定し、
Discord Webhook に通知するスクリプト。

Phase2更新:
- シグナルをSQLite(db_utils)に記録し、後日の的中率検証に使えるようにした
- クールダウン(同一資産・同一方向は一定時間以内は再通知しない)を追加
- リスク計算(参考ポジションサイズ・損切りライン)を通知に追加

注意:
- これは自動売買は行いません（通知のみ）。
- テクニカル指標に基づく単純な判定であり、投資助言ではありません。
- 実際の売買判断は自己責任で行ってください。
"""

import os
import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import pandas as pd

import db_utils
import risk_utils

# ============ 設定 ============

COINS = [
    {"id": "bitcoin", "symbol": "BTC"},
    {"id": "ethereum", "symbol": "ETH"},
    {"id": "solana", "symbol": "SOL"},
    {"id": "ripple", "symbol": "XRP"},
    {"id": "hyperliquid", "symbol": "HYPE"},
    {"id": "dogecoin", "symbol": "DOGE"},
    {"id": "edgex", "symbol": "EDGE"},
    {"id": "tria", "symbol": "TRIA"},
    {"id": "sui", "symbol": "SUI"},
    {"id": "aave", "symbol": "AAVE"},
]

VS_CURRENCY = "usd"
DAYS = 7
SMA_SHORT = 9
SMA_LONG = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

COOLDOWN_MINUTES = int(os.environ.get("CRYPTO_COOLDOWN_MINUTES", "180"))  # 同一資産・同一方向の再通知間隔

# was_recently_notified はsource+asset+directionで判定するため、方向が反転した瞬間は
# クールダウンを素通りしてしまう(LONG<->SHORTの切り替えは「別方向」扱いになるため)。
# レンジ相場でSMAクロスが小刻みに反転する「ダマシ」への往復ビンタ通知を減らすため、
# 直近CONFIRM_COUNT回連続で同じ方向が出た場合のみ通知する確認フィルタを設ける。
CONFIRM_COUNT = int(os.environ.get("CRYPTO_CONFIRM_COUNT", "2"))
STATE_FILE = Path("crypto_signal_state.json")

# 押し目シグナル: トレンド方向(SMA)に関わらず、RSIが売られすぎ圏に入ったこと自体を
# 現物の買い候補として知らせる(トレンドフォローのLONG/SHORTとは独立した別軸の指標)
DIP_RSI_THRESHOLD = float(os.environ.get("DIP_RSI_THRESHOLD", str(RSI_OVERSOLD)))
SOURCE_DIP = "crypto_dip"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

SOURCE_NAME = "crypto_technical"


# ============ 状態の読み書き(連続確認カウントの保持) ============

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ============ データ取得 ============

def fetch_price_history(coin_id: str, vs_currency: str = "usd", days: int = 7) -> pd.DataFrame:
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": vs_currency, "days": days}

    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code == 429:
        # 銘柄数が増えたことでレート制限に当たることがあるため、少し待って再試行する(最大2回)
        for _ in range(2):
            time.sleep(30)
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 429:
                break
    resp.raise_for_status()
    data = resp.json()

    prices = data.get("prices", [])
    df = pd.DataFrame(prices, columns=["timestamp_ms", "price"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.set_index("timestamp").drop(columns=["timestamp_ms"])
    return df


# ============ 指標計算 ============

def compute_sma(df: pd.DataFrame, short: int, long: int) -> pd.DataFrame:
    df = df.copy()
    df["sma_short"] = df["price"].rolling(window=short).mean()
    df["sma_long"] = df["price"].rolling(window=long).mean()
    return df


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df = df.copy()
    delta = df["price"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    df["rsi"] = rsi
    return df


# ============ シグナル判定 ============

def judge_signal(df: pd.DataFrame) -> dict:
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    sma_short_now, sma_long_now = latest["sma_short"], latest["sma_long"]
    sma_short_prev, sma_long_prev = prev["sma_short"], prev["sma_long"]
    rsi_now = latest["rsi"]
    price_now = latest["price"]

    if pd.isna(sma_short_now) or pd.isna(sma_long_now) or pd.isna(rsi_now):
        return {"signal": "データ不足", "price": price_now, "rsi": rsi_now}

    golden_cross = sma_short_prev <= sma_long_prev and sma_short_now > sma_long_now
    dead_cross = sma_short_prev >= sma_long_prev and sma_short_now < sma_long_now
    trend_up = sma_short_now > sma_long_now
    trend_down = sma_short_now < sma_long_now

    if (golden_cross or trend_up) and rsi_now < RSI_OVERBOUGHT:
        signal = "LONG"
    elif (dead_cross or trend_down) and rsi_now > RSI_OVERSOLD:
        signal = "SHORT"
    else:
        signal = "様子見"

    return {
        "signal": signal, "price": price_now, "rsi": rsi_now,
        "sma_short": sma_short_now, "sma_long": sma_long_now,
        "golden_cross": golden_cross, "dead_cross": dead_cross,
    }


# ============ 通知 ============

def build_discord_message(results: list) -> str:
    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"**🪙 仮想通貨シグナル通知 ({now_str})**", ""]

    active_signal_count = sum(1 for r in results if r["result"]["signal"] in ("LONG", "SHORT"))
    caution = risk_utils.format_multi_signal_caution(active_signal_count)
    if caution:
        lines.append(caution)
        lines.append("")

    has_short = any(r["result"]["signal"] == "SHORT" for r in results)
    if has_short:
        lines.append("ℹ️ 現物運用のため「SHORT」は空売りではなく「保有中なら売却/未保有なら買い見送り」の意味です。")
        lines.append("")

    for r in results:
        symbol = r["symbol"]
        signal = r["result"]["signal"]

        if signal == "LONG":
            emoji = "🟢"
        elif signal == "SHORT":
            emoji = "🔴"
        elif signal.startswith("様子見"):
            emoji = "⚪"
        else:
            emoji = "⚠️"

        price = r["result"].get("price")
        rsi = r["result"].get("rsi")
        price_str = f"${price:,.2f}" if price is not None else "N/A"
        rsi_str = f"{rsi:.1f}" if rsi is not None and pd.notna(rsi) else "N/A"

        lines.append(f"{emoji} **{symbol}**: {signal} | 価格 {price_str} | RSI {rsi_str}")

        if signal in ("LONG", "SHORT") and price is not None:
            risk_line = risk_utils.format_risk_line(price, signal)
            if risk_line:
                lines.append(f"　{risk_line}")

    lines.append("")
    lines.append("_※テクニカル指標による簡易判定です。投資助言ではありません。_")
    lines.append("_Data provided by CoinGecko (https://www.coingecko.com)_")
    return "\n".join(lines)


# 押し目「反転」シグナル用の演出色(警告レッド)。パトランプ的な視覚効果を
# Discord Embedの縦バーで表現する。
AWAKENING_COLOR = 0xFF3B30


def build_awakening_payload(dip_signals: list) -> dict:
    """押し目からの反転検知を、通常のLONG/SHORT通知とは一目で区別できる
    派手めのDiscord Embedとして組み立てる。"""
    fields = []
    for d in dip_signals:
        value_lines = [f"RSI {d['prev_rsi']:.0f} → {d['rsi']:.0f}(反転上昇)", f"価格 ${d['price']:,.2f}"]
        risk_line = risk_utils.format_risk_line(d["price"], "LONG")
        if risk_line:
            value_lines.append(risk_line)
        fields.append({"name": f"🔴 {d['symbol']}", "value": "\n".join(value_lines), "inline": False})

    embed = {
        "title": "⚡ 覚醒シグナル AWAKENING MODE ⚡",
        "description": "売られすぎ圏からの反転初動を検知しました。現物での買い候補の参考情報です。",
        "color": AWAKENING_COLOR,
        "fields": fields,
        "footer": {"text": "※反転の初動を捉えた簡易判定です。ダマシ(だまし上げ)の可能性もあります。投資助言ではありません。"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = "🚨🔴🚨 **覚醒シグナル検知** 🚨🔴🚨"
    return {"content": content, "embeds": [embed]}


def send_awakening_notification(dip_signals: list):
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL が設定されていないため、コンソールに出力のみ行います。")
        for d in dip_signals:
            print(f"🔴 覚醒: {d['symbol']} RSI {d['prev_rsi']:.0f}->{d['rsi']:.0f} 価格${d['price']:,.2f}")
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json=build_awakening_payload(dip_signals), timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] 覚醒シグナル通知に失敗しました: {resp.status_code} {resp.text}")
    else:
        print("[OK] 覚醒シグナル通知を送信しました。")


def send_discord_notification(message: str):
    if not DISCORD_WEBHOOK_URL:
        print("[警告] DISCORD_WEBHOOK_URL が設定されていないため、コンソールに出力のみ行います。")
        print(message)
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    if resp.status_code >= 300:
        print(f"[エラー] Discord通知に失敗しました: {resp.status_code} {resp.text}")
    else:
        print("[OK] Discordに通知を送信しました。")


# ============ メイン処理 ============

def main():
    db_utils.init_db()
    state = load_state()
    confirm_state = state.setdefault("confirm", {})
    dip_rsi_state = state.setdefault("dip_rsi", {})
    results = []
    dip_signals = []
    any_to_notify = False

    for coin in COINS:
        coin_id = coin["id"]
        symbol = coin["symbol"]
        try:
            df = fetch_price_history(coin_id, VS_CURRENCY, DAYS)
            df = compute_sma(df, SMA_SHORT, SMA_LONG)
            df = compute_rsi(df, RSI_PERIOD)
            result = judge_signal(df)
            raw_signal = result["signal"]

            if raw_signal in ("LONG", "SHORT"):
                # 直近CONFIRM_COUNT回連続で同じ方向かを確認(ダマシによる往復ビンタ通知を防止)
                prev = confirm_state.get(symbol, {})
                streak = prev.get("count", 0) + 1 if prev.get("last_signal") == raw_signal else 1
                confirm_state[symbol] = {"last_signal": raw_signal, "count": streak}

                if streak < CONFIRM_COUNT:
                    print(f"{symbol}: {raw_signal}(確認{streak}/{CONFIRM_COUNT}回目のため様子見扱い)")
                    result["signal"] = f"様子見(確認中{streak}/{CONFIRM_COUNT})"
                elif db_utils.was_recently_notified(SOURCE_NAME, symbol, raw_signal, COOLDOWN_MINUTES):
                    print(f"{symbol}: {raw_signal} だがクールダウン中のためスキップ")
                    result["signal"] = "様子見(クールダウン中)"
                else:
                    db_utils.log_signal(SOURCE_NAME, symbol, raw_signal, result["price"], message=f"RSI{result['rsi']:.0f}")
                    any_to_notify = True
            else:
                # 様子見/データ不足時は連続確認カウントをリセット(方向転換の再確認をやり直す)
                confirm_state.pop(symbol, None)

            # 押し目チェック: 単に「売られすぎ圏(RSI<=閾値)」なだけでなく、
            # 前回チェック時よりRSIが上向いた(反転の初動)場合のみ発火させる。
            # 「落ちてるナイフ」(下げ止まっていないのに安いというだけで飛びつく)を避けるため。
            rsi_val = result.get("rsi")
            price_val = result.get("price")
            if rsi_val is not None and pd.notna(rsi_val) and price_val is not None:
                prev_rsi = dip_rsi_state.get(symbol)
                is_reversal = (
                    rsi_val <= DIP_RSI_THRESHOLD
                    and prev_rsi is not None
                    and rsi_val > prev_rsi
                )
                if is_reversal:
                    if db_utils.was_recently_notified(SOURCE_DIP, symbol, "LONG", COOLDOWN_MINUTES):
                        print(f"{symbol}: 押し目反転候補(RSI{prev_rsi:.0f}→{rsi_val:.0f}) だがクールダウン中のためスキップ")
                    else:
                        db_utils.log_signal(SOURCE_DIP, symbol, "LONG", price_val,
                                             message=f"押し目反転 RSI{prev_rsi:.0f}→{rsi_val:.0f}")
                        dip_signals.append({"symbol": symbol, "price": price_val, "rsi": rsi_val, "prev_rsi": prev_rsi})
                dip_rsi_state[symbol] = rsi_val  # 次回比較のため常に更新

            results.append({"symbol": symbol, "result": result})
            print(f"{symbol}: {result['signal']}")
        except Exception as e:
            print(f"[エラー] {symbol} の取得/計算に失敗しました: {e}")
            results.append({"symbol": symbol, "result": {"signal": "取得エラー", "price": None, "rsi": None}})

        time.sleep(6)

    save_state(state)

    if any_to_notify:
        message = build_discord_message(results)
        send_discord_notification(message)
    else:
        print("新規のLONG/SHORTシグナルがないため、通知はスキップしました。")

    if dip_signals:
        send_awakening_notification(dip_signals)
    else:
        print("新規の押し目シグナルはありません。")


if __name__ == "__main__":
    sys.exit(main())
