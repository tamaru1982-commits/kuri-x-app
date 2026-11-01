"""日足バックテストの共通部品。kuri-x-appのシグナル判定・ペーパートレードのルールを再現する。

価格はyfinanceから取得し、backtest/bt_cache/ にCSVで保存する(2回目以降は再取得しない。
最新データで検証し直すときはbt_cacheを消す)。
"""
from pathlib import Path
import numpy as np
import pandas as pd

CACHE = Path(__file__).parent / "bt_cache"
CACHE.mkdir(exist_ok=True)

TICKERS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD",
    "SUI": "SUI20947-USD", "AAVE": "AAVE-USD", "HYPE": "HYPE32196-USD",
}
# one-way fee %: domestic exchange vs DEX swap
FEE = {"BTC": 0.15, "ETH": 0.15, "XRP": 0.15, "SOL": 0.15, "DOGE": 0.15,
       "SUI": 0.5, "AAVE": 0.5, "HYPE": 0.5}


def load(asset: str) -> pd.DataFrame:
    f = CACHE / f"{asset}.csv"
    if f.exists():
        df = pd.read_csv(f, index_col=0, parse_dates=True)
    else:
        import yfinance as yf
        d = yf.download(TICKERS[asset], period="max", interval="1d", progress=False, auto_adjust=False)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        df = d[["Open", "High", "Low", "Close"]].dropna()
        df.to_csv(f)
    # drop today's incomplete bar
    return df[df.index < pd.Timestamp.today().normalize()]


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["Close"]
    df["sma9"] = c.rolling(9).mean()
    df["sma21"] = c.rolling(21).mean()
    df["sma50"] = c.rolling(50).mean()
    df["sma200"] = c.rolling(200).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + g / l.replace(0, np.nan))
    df.loc[df.index[:14], "rsi"] = np.nan
    df["vol30"] = np.log(c).diff().rolling(30).std() * 100
    df["high20"] = df["High"].rolling(20).max().shift(1)   # prior 20-day high
    df["golden"] = (df["sma9"] > df["sma21"]) & (df["sma9"].shift(1) <= df["sma21"].shift(1))
    df["dead"] = (df["sma9"] < df["sma21"]) & (df["sma9"].shift(1) >= df["sma21"].shift(1))
    return df


def stop_target(vol: float, stop_mult=1.2, ratio=1.6667):
    stop = min(8.0, max(2.0, vol * stop_mult)) if vol and vol > 0 else 3.0
    return stop, stop * ratio


def simulate(df: pd.DataFrame, entries: pd.Series, asset: str, max_hold=3, stop_mult=1.2, ratio=1.6667,
             exit_on_dead=True, trailing=None, one_at_a_time=True):
    """entries: bool Series (signal at close of that day -> buy at that close).
    trailing: None for fixed target, or trailing-stop % multiple of vol (no target, no time limit unless max_hold)."""
    fee = FEE[asset]
    idx = df.index
    pos_of = {t: i for i, t in enumerate(idx)}
    trades = []
    busy_until = -1
    for t in idx[entries.reindex(idx).fillna(False).to_numpy()]:
        i = pos_of[t]
        if one_at_a_time and i <= busy_until:
            continue
        row = df.iloc[i]
        if pd.isna(row["vol30"]):
            continue
        entry = row["Close"]
        stop_pct, tgt_pct = stop_target(row["vol30"], stop_mult, ratio)
        stop_px = entry * (1 - stop_pct / 100)
        tgt_px = entry * (1 + tgt_pct / 100)
        exit_px = None; reason = None; j = i
        peak = entry
        last = min(len(df) - 1, i + max_hold) if max_hold else len(df) - 1
        for j in range(i + 1, last + 1):
            b = df.iloc[j]
            if trailing is not None:
                trail_px = peak * (1 - trailing * row["vol30"] / 100)
                eff_stop = max(stop_px, trail_px)
                if b["Low"] <= eff_stop:
                    exit_px = min(eff_stop, b["Open"]); reason = "stop"; break
                peak = max(peak, b["High"])
            else:
                if b["Low"] <= stop_px:                 # stop first if both touched (conservative)
                    exit_px = min(stop_px, b["Open"]); reason = "stop"; break
                if b["High"] >= tgt_px:
                    exit_px = max(tgt_px, b["Open"]) if b["Open"] > tgt_px else tgt_px; reason = "target"; break
            if exit_on_dead and b["dead"]:
                exit_px = b["Close"]; reason = "dead"; break
        if exit_px is None:
            if j == i or (max_hold and j < i + max_hold and j == len(df) - 1):
                continue  # not enough data yet
            exit_px = df.iloc[j]["Close"]; reason = "time"
        gross = (exit_px / entry - 1) * 100
        net = ((exit_px * (1 - fee / 100)) / (entry * (1 + fee / 100)) - 1) * 100
        trades.append({"asset": asset, "date": t, "exit_date": idx[j], "days": j - i,
                       "gross": gross, "net": net, "reason": reason,
                       "regime_up": bool(row["Close"] > row["sma200"]) if not pd.isna(row["sma200"]) else None})
        busy_until = j
    return pd.DataFrame(trades)


def stats(tr: pd.DataFrame) -> dict:
    if tr is None or len(tr) == 0:
        return {"n": 0}
    n = len(tr); m = tr["net"].mean(); sd = tr["net"].std(ddof=1) if n > 1 else np.nan
    eq = (1 + tr.sort_values("date")["net"] / 100).cumprod()
    dd = (eq / eq.cummax() - 1).min() * 100
    return {"n": n, "win%": round((tr["net"] > 0).mean() * 100, 1), "avg%": round(m, 2),
            "med%": round(tr["net"].median(), 2), "t": round(m / sd * np.sqrt(n), 2) if n > 1 and sd > 0 else np.nan,
            "target%": round((tr["reason"] == "target").mean() * 100), "stop%": round((tr["reason"] == "stop").mean() * 100),
            "maxDD%": round(dd, 1)}
