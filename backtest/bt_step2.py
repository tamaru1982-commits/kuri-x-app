"""Step 2: 事前に決めた戦略を、資産推移(年率リターン・最大下落幅・保有期間の割合)で比べる。
判定はすべて日足の終値。手数料は売買のたびに片道分を差し引く。
引数はトレーリングストップの幅(日次変動率の何倍か、既定3)。

    python backtest/bt_step2.py 2
"""
import sys, warnings
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import bt_engine as E

TRAIL_K = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
START, SPLIT = "2019-01-01", "2023-01-01"

def positions(df: pd.DataFrame, strat: str) -> pd.Series:
    """Return 0/1 holding series (holding from close of day t into t+1)."""
    c = df["Close"].to_numpy(); n = len(df)
    sma200 = df["sma200"].to_numpy(); vol = df["vol30"].to_numpy(); rsi = df["rsi"].to_numpy()
    golden = df["golden"].to_numpy(); high20 = df["high20"].to_numpy()
    up = c > sma200  # NaN sma200 -> False
    if strat == "A":
        return pd.Series(1.0, index=df.index)
    if strat == "B":
        return pd.Series(up.astype(float), index=df.index)
    entry_sig = {
        "C": golden,
        "C+": golden & up,
        "D": c > high20,
        "D+": (c > high20) & up,
        "E": up & (np.roll(rsi, 1) <= 40) & (rsi > np.roll(rsi, 1)),
    }[strat]
    pos = np.zeros(n); holding = False; peak = 0.0; k_vol = 0.0
    for i in range(n):
        if holding:
            peak = max(peak, c[i])
            if c[i] <= peak * (1 - k_vol / 100):
                holding = False
        elif entry_sig[i] and not np.isnan(vol[i]):
            holding = True; peak = c[i]; k_vol = TRAIL_K * vol[i]
        pos[i] = 1.0 if holding else 0.0
    return pd.Series(pos, index=df.index)

def daily_returns(df, pos, fee):
    r = df["Close"].pct_change().fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    changes = pos.diff().abs().fillna(pos.iloc[0])
    return held * r - changes * fee / 100

def metrics(ret: pd.Series, pos: pd.Series | None = None) -> dict:
    if len(ret) < 30:
        return None
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 365.25
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = (eq / eq.cummax() - 1).min() * 100
    out = {"CAGR": cagr, "maxDD": dd, "total": (eq.iloc[-1] - 1) * 100}
    if pos is not None:
        out["expo"] = pos.mean() * 100
        out["trades"] = int((pos.diff() == 1).sum() + (pos.iloc[0] == 1))
    return out

STRATS = ["A", "B", "C", "C+", "D", "D+", "E"]
data = {a: E.add_indicators(E.load(a)) for a in E.TICKERS}
per = {}   # (asset, strat) -> (ret, pos) over full START~
for a, df in data.items():
    df = df[df.index >= START]
    df = df[df["sma200"].notna()]          # need 200d warmup for B/E/+ to be comparable
    for s in STRATS:
        pos = positions(df, s)
        per[(a, s)] = (daily_returns(df, pos, E.FEE[a]), pos)

def window(lo, hi):
    def f(x): return x[(x.index >= lo) & (x.index < hi)]
    return f

PERIODS = {"前半 2019-22": window(START, SPLIT), "後半 2023-": window(SPLIT, "2100-01-01"),
           "2022年(下落)": window("2022-01-01", "2023-01-01")}

for pname, f in PERIODS.items():
    print(f"\n######## {pname}  (trail k={TRAIL_K})")
    print(f"{'strat':5s} | {'EW portfolio CAGR':>17s} {'maxDD':>7s} | {'assets: median CAGR':>19s} {'median maxDD':>12s} {'expo%':>6s} {'trades/asset':>12s}")
    for s in STRATS:
        rets, rows = [], []
        for a in E.TICKERS:
            if (a, s) not in per: continue
            r, p = per[(a, s)]; r, p = f(r), f(p)
            m = metrics(r, p)
            if m is None: continue
            rows.append(m); rets.append(r.rename(a))
        if not rows: continue
        port = pd.concat(rets, axis=1).mean(axis=1, skipna=True)   # equal weight over available assets
        pm = metrics(port)
        dfm = pd.DataFrame(rows)
        print(f"{s:5s} | {pm['CAGR']:+16.0f}% {pm['maxDD']:+6.0f}% | {dfm.CAGR.median():+18.0f}% {dfm.maxDD.median():+11.0f}% "
              f"{dfm.expo.median():5.0f}% {dfm.trades.median():12.0f}")

print("\n######## per asset, 後半 2023- : CAGR / maxDD")
f = PERIODS["後半 2023-"]
print("asset  " + "  ".join(f"{s:>13s}" for s in STRATS))
for a in E.TICKERS:
    cells = []
    for s in STRATS:
        r, p = per[(a, s)]; m = metrics(f(r), f(p))
        cells.append(f"{m['CAGR']:+5.0f}/{m['maxDD']:+4.0f}" if m else "-")
    print(f"{a:6s} " + "  ".join(f"{c:>13s}" for c in cells))
