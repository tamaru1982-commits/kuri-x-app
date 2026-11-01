"""Step 1: 今のシグナル(tech=SMA9/21クロス, dip=RSI反転)と今のペーパートレードの売り方を
過去の日足で検証し、「毎日どの日に買っても」の基準と比べる。

    python backtest/bt_step1.py
"""
import sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
import pandas as pd
from bt_engine import TICKERS, load, add_indicators, simulate, stats

START = "2019-01-01"   # common-ish window; earlier BTC history is a different market
SPLIT = "2023-01-01"
data = {a: add_indicators(load(a)) for a in TICKERS}

def rules(df):
    tech = df["golden"] & (df["rsi"] < 70)
    dip = (df["rsi"] <= 30) & (df["rsi"] > df["rsi"].shift(1))
    every = pd.Series(True, index=df.index)
    return {"tech": tech, "dip": dip, "baseline(every day)": every}

all_tr = {k: [] for k in ["tech", "dip", "baseline(every day)"]}
for a, df in data.items():
    df = df[df.index >= START]
    for name, ent in rules(df).items():
        tr = simulate(df, ent, a, one_at_a_time=(name != "baseline(every day)"))
        if len(tr): all_tr[name].append(tr)

def show(title, f):
    print(f"\n=== {title}")
    print(f"{'rule':22s} {'n':>5s} {'win%':>6s} {'avg%':>6s} {'med%':>6s} {'t':>6s} {'tgt%':>5s} {'stop%':>5s}")
    for name, lst in all_tr.items():
        tr = pd.concat(lst); tr = f(tr)
        s = stats(tr)
        if s["n"] == 0: continue
        print(f"{name:22s} {s['n']:5d} {s['win%']:6.1f} {s['avg%']:+6.2f} {s['med%']:+6.2f} {s['t']:6.2f} {s['target%']:5d} {s['stop%']:5d}")

show(f"ALL assets, {START}~", lambda t: t)
show(f"In-sample  {START}~{SPLIT}", lambda t: t[t.date < SPLIT])
show(f"Out-of-sample {SPLIT}~", lambda t: t[t.date >= SPLIT])
show("BTC regime: asset above its 200DMA", lambda t: t[t.regime_up == True])
show("asset below its 200DMA", lambda t: t[t.regime_up == False])

print("\n=== per asset (avg net % per trade; n)")
print(f"{'asset':6s} {'tech':>14s} {'dip':>14s} {'baseline':>14s} {'B&H CAGR':>9s}")
for a, df in data.items():
    df = df[df.index >= START]
    out = []
    for name, ent in rules(df).items():
        tr = simulate(df, ent, a, one_at_a_time=(name != "baseline(every day)"))
        s = stats(tr); out.append(f"{s.get('avg%', float('nan')):+6.2f} ({s['n']:4d})")
    yrs = (df.index[-1] - df.index[0]).days / 365.25
    cagr = ((df["Close"].iloc[-1] / df["Close"].iloc[0]) ** (1 / yrs) - 1) * 100
    print(f"{a:6s} {out[0]:>14s} {out[1]:>14s} {out[2]:>14s} {cagr:+8.0f}%")

print("\n=== exit reasons (tech/dip)")
for name in ["tech", "dip"]:
    tr = pd.concat(all_tr[name]); print(name, tr.reason.value_counts(normalize=True).round(2).to_dict())
