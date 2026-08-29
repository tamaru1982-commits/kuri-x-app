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

# CoinGeckoは日数で足の粒度が変わる(2〜90日=1時間足、91日以上=日足)。
# 以前はDAYS=7だったため1時間足で、SMA9/21は「9時間平均と21時間平均」を意味しており、
# 「1〜3日以内に+5%」という運用の時間軸に対して短すぎた(クロスが週7.7回/銘柄も発生)。
# 日足に変更し、SMA9/21を本来想定される「9日平均と21日平均」にする。
DAYS = 120
SMA_SHORT = 9
SMA_LONG = 21
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

COOLDOWN_MINUTES = int(os.environ.get("CRYPTO_COOLDOWN_MINUTES", "180"))  # 同一資産・同一方向の再通知間隔

# レンジ相場でSMAクロスが小刻みに反転する「ダマシ」を弾くための確認フィルタ。
#
# 以前は「直近2回連続で同じ方向が出たら通知」という実装だったが、これは判定が
# 「トレンドが継続している状態」で真になる状態ベースだったから成立していた。
# クロス(一瞬の出来事)のみを見る現在の判定では、次の実行までにローソク足が進むと
# クロス条件自体が消えるため、同じ条件では通知がほとんど出なくなる。
# そのため「クロスを検知した回はいったん保留し、次の実行でもまだ同じ側にいれば確定」
# という形にしている(クロス直後に押し戻された場合は見送られる)。
# CONFIRM_CROSS=0 にすると保留せずクロス検知した回にそのまま通知する。
CONFIRM_CROSS = os.environ.get("CRYPTO_CONFIRM_CROSS", "1") not in ("0", "false", "False")

# クロスを直近何本(=日足なので何日)ぶんさかのぼって探すか。
# 直近2点だけを見る実装だと、GitHub Actionsの実行が遅延・欠落した回にクロスが
# 起きていた場合、そのクロスを二度と検知できない(トレンド継続で判定していた頃は
# 次の実行で拾えたが、イベントで判定する以上は取りこぼしがそのまま欠損になる)。
# 同じクロスを何度も通知しないよう、処理済みのクロスは発生時刻で識別して除外する
# (クールダウンだけでは、期限が切れるたびに同じクロスを再通知してしまう)。
CROSS_LOOKBACK = int(os.environ.get("CRYPTO_CROSS_LOOKBACK", "2"))
STATE_FILE = Path("crypto_signal_state.json")

# 押し目シグナル: トレンド方向(SMA)に関わらず、RSIが売られすぎ圏に入ったこと自体を
# 現物の買い候補として知らせる(トレンドフォローのLONG/SHORTとは独立した別軸の指標)
DIP_RSI_THRESHOLD = float(os.environ.get("DIP_RSI_THRESHOLD", str(RSI_OVERSOLD)))

# 押し目シグナルは日足のRSI反転を見るため、同じ足で何度も鳴らないよう
# 通常より長いクールダウン(既定24時間=1日1回まで)にする。
DIP_COOLDOWN_MINUTES = int(os.environ.get("DIP_COOLDOWN_MINUTES", "1440"))
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


def compute_daily_volatility_pct(df: pd.DataFrame, lookback: int = 30) -> float | None:
    """日次の変動率(対数リターンの標準偏差, %)を求める。

    銘柄ごとに損切り幅を決めるために使う。DAYSが日足の粒度になっているため、
    そのままの足の標準偏差が日次変動率になる。
    この値はstate経由でpaper_trader.pyにも渡され、追加のAPI呼び出しなしに
    全スクリプトが同じ基準で損切り幅を決められるようにしている。
    """
    import numpy as np

    prices = df["price"].tail(lookback + 1).to_numpy()
    if len(prices) < 5 or (prices <= 0).any():
        return None
    returns = np.diff(np.log(prices))
    vol = float(np.std(returns) * 100)
    return vol if vol > 0 else None


def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df = df.copy()
    delta = df["price"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # RSIは単純移動平均ではなくWilderの指数平滑(alpha=1/period)で計算するのが標準。
    # 単純移動平均で計算すると値が振れやすくなり、70/30という閾値は標準RSI向けに
    # 決められた基準なので、実質的に緩い判定になってしまう。
    # (BTC 1時間足7日で比較したところ最大21ポイント乖離し、「買われすぎ」の検知回数が
    #  標準の7倍、「売られすぎ」が2倍になっていた=dipシグナルが出過ぎていた)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    # 指数平滑は最初の1本から値が出てしまうが、period本に満たない区間の値は
    # 信頼できないため、従来どおり「データ不足」として扱えるようNaNにする。
    avg_gain = avg_gain.mask(avg_gain.index < avg_gain.index[min(period, len(avg_gain) - 1)])
    avg_loss = avg_loss.mask(avg_loss.index < avg_loss.index[min(period, len(avg_loss) - 1)])

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))

    # 期間中に一度も下落がないと avg_loss が0になり、上の計算ではRSIがNaNになる。
    # NaNのままだと judge_signal が「データ不足」として扱い、本来なら最も強い
    # 上昇局面のシグナルが握り潰されてしまうため、定義どおりの値を明示的に入れる。
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)   # 全て上昇 = 買われすぎの極
    rsi = rsi.mask((avg_loss == 0) & (avg_gain == 0), 50.0)   # 値動きなし = 中立

    df["rsi"] = rsi
    return df


# ============ シグナル判定 ============

def detect_recent_cross(df: pd.DataFrame, lookback: int) -> tuple[str | None, str | None]:
    """直近lookback本ぶんの区間で最後に起きたSMAクロスを (方向, 発生時刻) で返す。

    実行の遅延・欠落でクロスを取りこぼさないよう検知に幅を持たせているが、
    幅を持たせると同じクロスが複数回の実行で検知されることになる。
    そのままだとクールダウンが切れるたびに同じクロスを再通知してしまい、
    「同じ相場を何度もシグナル化する」という以前と同じ問題が再発するため、
    どのクロスなのかを識別できるよう発生時刻も併せて返す。

    区間内に複数回クロスした場合は最後の向きを採用する
    (往復した場合、現時点で意味を持つのは直近の向きのため)。
    """
    direction = cross_at = None
    window = df.iloc[-(lookback + 1):]
    for i in range(1, len(window)):
        prev, now = window.iloc[i - 1], window.iloc[i]
        if any(pd.isna(v) for v in (prev["sma_short"], prev["sma_long"], now["sma_short"], now["sma_long"])):
            continue
        if prev["sma_short"] <= prev["sma_long"] and now["sma_short"] > now["sma_long"]:
            direction, cross_at = "LONG", str(window.index[i])
        elif prev["sma_short"] >= prev["sma_long"] and now["sma_short"] < now["sma_long"]:
            direction, cross_at = "SHORT", str(window.index[i])
    return direction, cross_at


def judge_signal(df: pd.DataFrame) -> dict:
    latest = df.iloc[-1]

    sma_short_now, sma_long_now = latest["sma_short"], latest["sma_long"]
    rsi_now = latest["rsi"]
    price_now = latest["price"]

    if pd.isna(sma_short_now) or pd.isna(sma_long_now) or pd.isna(rsi_now):
        return {"signal": "データ不足", "price": price_now, "rsi": rsi_now}

    cross_dir, cross_at = detect_recent_cross(df, CROSS_LOOKBACK)
    golden_cross = cross_dir == "LONG"
    dead_cross = cross_dir == "SHORT"
    trend_up = sma_short_now > sma_long_now
    trend_down = sma_short_now < sma_long_now

    # クロスした瞬間のみをシグナルとして扱う(トレンド継続中は出さない)。
    # 以前は (golden_cross or trend_up) としていたため、トレンドが続いている限り
    # 同じ相場が何度もシグナル化され、実質1つの相場への賭けを複数回検証してしまっていた
    # (実測: ETH LONG 5回 / XRP SHORT 5回)。売買タイミングとして意味を持つのは
    # クロスした瞬間だけなので、そこに絞る。
    if golden_cross and rsi_now < RSI_OVERBOUGHT:
        signal = "LONG"
    elif dead_cross and rsi_now > RSI_OVERSOLD:
        signal = "SHORT"
    else:
        signal = "様子見"

    return {
        "signal": signal, "price": price_now, "rsi": rsi_now,
        "sma_short": sma_short_now, "sma_long": sma_long_now,
        "golden_cross": golden_cross, "dead_cross": dead_cross,
        "trend_up": trend_up, "trend_down": trend_down,
        "cross_at": cross_at,
    }


# ============ 通知 ============

def build_discord_message(results: list, volatility: dict | None = None) -> str:
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
            vol = (volatility or {}).get(symbol)
            stop_pct = risk_utils.stop_loss_pct_for_volatility(vol)
            risk_line = risk_utils.format_risk_line(price, signal, stop_pct)
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
        stop_pct = risk_utils.stop_loss_pct_for_volatility(d.get("volatility"))
        risk_line = risk_utils.format_risk_line(d["price"], "LONG", stop_pct)
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

    # 旧形式({"last_signal":..,"count":N})の残骸を掃除する。
    # 現在は{"pending":方向}のみを使うため、そのままだと解除されず残り続ける。
    for symbol in [s for s, v in confirm_state.items() if not isinstance(v, dict) or not ({"pending", "handled_cross_at"} & set(v))]:
        confirm_state.pop(symbol, None)
    # さらに古い版がstate直下に書いていた銘柄キーも掃除する
    for key in [k for k in state if k not in ("confirm", "volatility")]:
        state.pop(key, None)

    # 銘柄ごとの日次変動率。paper_trader.pyが損切り幅を決めるのに使う
    volatility_state = state.setdefault("volatility", {})
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

            # 損切り幅の算出に使うため、銘柄ごとの日次変動率を毎回更新しておく
            vol = compute_daily_volatility_pct(df)
            if vol is not None:
                volatility_state[symbol] = round(vol, 3)
            result = judge_signal(df)
            raw_signal = result["signal"]

            # クロス検知 → 次回も同じ側にいれば確定、という2段階で判定する。
            # 確定した方向をfire_signalに入れる(Noneなら今回は通知しない)。
            fire_signal = None
            sym_state = confirm_state.get(symbol) or {}
            pending_dir = sym_state.get("pending")
            pending_at = sym_state.get("cross_at")
            handled_at = sym_state.get("handled_cross_at")
            cross_at = result.get("cross_at")

            # 検知に幅を持たせている以上、同じクロスが複数回の実行で検知される。
            # 一度処理したクロスは発生時刻で識別してスキップしないと、
            # クールダウンが切れるたびに同じクロスを再通知してしまう。
            if raw_signal in ("LONG", "SHORT") and cross_at and cross_at == handled_at:
                raw_signal = "様子見"

            # 今回検知したクロス、または前回保留したクロスを候補とする
            if raw_signal in ("LONG", "SHORT"):
                candidate, candidate_at = raw_signal, cross_at
            else:
                candidate, candidate_at = pending_dir, pending_at

            if candidate:
                if not CONFIRM_CROSS:
                    fire_signal = candidate
                    confirm_state[symbol] = {"handled_cross_at": candidate_at}
                elif pending_dir == candidate and pending_at == candidate_at:
                    # 同じクロスの2回目の観測。まだ同じ側にいれば確定、押し戻されていれば見送り
                    still_same_side = result.get("trend_up") if candidate == "LONG" else result.get("trend_down")
                    confirm_state[symbol] = {"handled_cross_at": candidate_at}
                    if still_same_side:
                        fire_signal = candidate
                    else:
                        print(f"{symbol}: {candidate}クロスは押し戻されたため見送り(ダマシ)")
                        result["signal"] = f"様子見({candidate}クロスが続かず見送り)"
                else:
                    # 初回検知。次の実行でまだ同じ側にいるかを見てから判断する
                    confirm_state[symbol] = {"pending": candidate, "cross_at": candidate_at,
                                             "handled_cross_at": handled_at}
                    print(f"{symbol}: {candidate}クロス検知(次回も同じ側なら確定)")
                    result["signal"] = f"様子見(クロス確認中→{candidate})"

            if fire_signal:
                if db_utils.was_recently_notified(SOURCE_NAME, symbol, fire_signal, COOLDOWN_MINUTES):
                    print(f"{symbol}: {fire_signal} だがクールダウン中のためスキップ")
                    result["signal"] = "様子見(クールダウン中)"
                else:
                    result["signal"] = fire_signal
                    db_utils.log_signal(SOURCE_NAME, symbol, fire_signal, result["price"], message=f"RSI{result['rsi']:.0f}")
                    any_to_notify = True

            # 押し目チェック: 単に「売られすぎ圏(RSI<=閾値)」なだけでなく、
            # RSIが上向いた(反転の初動)場合のみ発火させる。
            # 「落ちてるナイフ」(下げ止まっていないのに安いというだけで飛びつく)を避けるため。
            #
            # 比較対象は「前回実行時のRSI」ではなく「1本前の足のRSI」を使う。
            # 日足に変更したことで、30分前の実行時点と比べてもRSIはほとんど動かず、
            # 実行間隔に依存した不安定な判定になってしまうため。
            rsi_val = result.get("rsi")
            price_val = result.get("price")
            prev_rsi = df["rsi"].iloc[-2] if len(df) >= 2 else None
            if (rsi_val is not None and pd.notna(rsi_val) and price_val is not None
                    and prev_rsi is not None and pd.notna(prev_rsi)):
                is_reversal = rsi_val <= DIP_RSI_THRESHOLD and rsi_val > prev_rsi
                if is_reversal:
                    if db_utils.was_recently_notified(SOURCE_DIP, symbol, "LONG", DIP_COOLDOWN_MINUTES):
                        print(f"{symbol}: 押し目反転候補(RSI{prev_rsi:.0f}→{rsi_val:.0f}) だがクールダウン中のためスキップ")
                    else:
                        db_utils.log_signal(SOURCE_DIP, symbol, "LONG", price_val,
                                             message=f"押し目反転 RSI{prev_rsi:.0f}→{rsi_val:.0f}")
                        dip_signals.append({"symbol": symbol, "price": price_val, "rsi": rsi_val,
                                            "prev_rsi": prev_rsi, "volatility": volatility_state.get(symbol)})

            results.append({"symbol": symbol, "result": result})
            print(f"{symbol}: {result['signal']}")
        except Exception as e:
            print(f"[エラー] {symbol} の取得/計算に失敗しました: {e}")
            results.append({"symbol": symbol, "result": {"signal": "取得エラー", "price": None, "rsi": None}})

        time.sleep(6)

    save_state(state)

    if any_to_notify:
        message = build_discord_message(results, volatility_state)
        send_discord_notification(message)
    else:
        print("新規のLONG/SHORTシグナルがないため、通知はスキップしました。")

    if dip_signals:
        send_awakening_notification(dip_signals)
    else:
        print("新規の押し目シグナルはありません。")


if __name__ == "__main__":
    sys.exit(main())
