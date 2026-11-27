"""
BACKTEST — Simulazione storica delle strategie
Esegui con: python3 backtest.py          (4 strategie storiche)
            python3 backtest.py orb      (confronto varianti ORB S&P500)
            python3 backtest.py azioni   (ORB su CAT e AVGO)
            python3 backtest.py trend    (ritracciamento nel trend, S&P500 giornaliero)
            python3 backtest.py trend_azioni   (stessa idea su CAT e AVGO)

VERSIONE CORRETTA:
 - PF = profitti netti / perdite nette su TUTTI i trade (anche quelli "OPEN",
   cioè chiusi a fine sessione senza toccare stop né target), costi inclusi.
 - Win rate = % di trade con P&L netto > 0.
 - Se non ci sono perdite il PF è "inf" (prima veniva diviso per 0.01).
 - Max drawdown calcolato includendo il capitale iniziale.
 - Nuova colonna "Open" con il numero di trade chiusi a fine sessione.
"""
import os
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import time
import pytz

ROME_TZ = pytz.timezone("Europe/Rome")
ET_TZ = pytz.timezone("America/New_York")

# Costo di spread andata+ritorno, in % del VALORE della posizione (non più € fissi).
# Fonti: schermata ordine CFD S&P 500 (≈0,0187%) e KID Revolut del 28/04/2026:
# indici ≈0,0042% (prudenziale 0,02%), azioni ≈0,0394%, materie prime ≈0,0375%.
# Oro (schermata ordine XAU:CFD): spread 0,14$ su 1.785,82$ di esposizione ≈ 0,0078%
#    (valore sotto prudenziale 0,01%). WTI: KID materie prime generico (≈0,0375%).
# Spread e tasso variano per orario e strumento: ri-controlla nell'app.
COSTS = {"SP500": 0.0002, "GOLD": 0.0001, "NASDAQ": 0.0002, "WTI": 0.0004, "AZIONI": 0.0004}
# Stress test dei costi: COSTI_X=2 python3 backtest.py orb   (moltiplica gli spread)
MOLTIPLICATORE_COSTI = float(os.environ.get("COSTI_X", "1") or 1)
CAPITAL = 1000.0
RISK_PCT = 0.01


# ── Helper ────────────────────────────────────────────────────────────────────
def get_data(ticker, period, interval):
    try:
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            print(f"  Dati insufficienti per {ticker}")
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df.dropna()
    except Exception as e:
        print(f"  Errore {ticker}: {e}")
        return None


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def atr_calc(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def bollinger(s, n=20, std=2.2):
    mid = s.rolling(n).mean()
    sig = s.rolling(n).std()
    return mid, mid + std * sig, mid - std * sig


def supertrend(df, n=10, mult=3.0):
    a = atr_calc(df, n)
    hl2 = (df["High"] + df["Low"]) / 2
    up = (hl2 + mult * a).copy()
    dn = (hl2 - mult * a).copy()
    trend = pd.Series(1, index=df.index)
    for i in range(1, len(df)):
        dn.iloc[i] = dn.iloc[i] if (dn.iloc[i] > dn.iloc[i-1] or df["Close"].iloc[i-1] < dn.iloc[i-1]) else dn.iloc[i-1]
        up.iloc[i] = up.iloc[i] if (up.iloc[i] < up.iloc[i-1] or df["Close"].iloc[i-1] > up.iloc[i-1]) else up.iloc[i-1]
        if df["Close"].iloc[i] > up.iloc[i-1]: trend.iloc[i] = 1
        elif df["Close"].iloc[i] < dn.iloc[i-1]: trend.iloc[i] = -1
        else: trend.iloc[i] = trend.iloc[i-1]
    st = pd.Series(np.where(trend == 1, dn, up), index=df.index)
    return st, trend


def calc_vwap(df):
    typ = (df["High"] + df["Low"] + df["Close"]) / 3
    df = df.copy()
    df["_tp"] = typ * df["Volume"]
    df["_d"] = df.index.date
    parts = []
    for d, g in df.groupby("_d"):
        parts.append(g["_tp"].cumsum() / g["Volume"].cumsum())
    return pd.concat(parts).reindex(df.index)


def resample_4h(df):
    return df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min",
                                  "Close": "last", "Volume": "sum"}).dropna()


# ── Simulatore (CORRETTO) ─────────────────────────────────────────────────────
def simulate(signals_df, cost_pct, label):
    capital = CAPITAL
    trades = []
    for _, row in signals_df.iterrows():
        entry, sl, tp, d = row["entry"], row["sl"], row["tp"], row["direction"]
        dist_sl = abs(entry - sl)
        if dist_sl == 0:
            continue
        risk_eur = capital * RISK_PCT
        next_close = row.get("next_close", entry)

        if d == 1:
            hit_tp = next_close >= tp
            hit_sl = next_close <= sl
        else:
            hit_tp = next_close <= tp
            hit_sl = next_close >= sl

        if hit_tp:
            pnl_pts = abs(tp - entry); outcome = "WIN"
        elif hit_sl:
            pnl_pts = -abs(entry - sl); outcome = "LOSS"
        else:
            pnl_pts = (next_close - entry) * d; outcome = "OPEN"

        size = risk_eur / dist_sl
        costo = size * entry * cost_pct * MOLTIPLICATORE_COSTI   # spread in % del valore posizione
        pnl_eur = pnl_pts * size - costo
        capital += pnl_eur
        trades.append({"dir": "LONG" if d == 1 else "SHORT", "outcome": outcome,
                       "pnl_eur": pnl_eur, "capital": capital})

    if not trades:
        return None

    df_t = pd.DataFrame(trades)

    # PF e win rate su TUTTI i trade, al netto dei costi
    winners = df_t["pnl_eur"] > 0
    gross_p = df_t.loc[winners, "pnl_eur"].sum()
    gross_l = abs(df_t.loc[~winners, "pnl_eur"].sum())
    pf = gross_p / gross_l if gross_l > 0 else float("inf")
    win_rate = winners.mean() * 100
    n_open = int((df_t["outcome"] == "OPEN").sum())

    # Drawdown: il picco parte dal capitale iniziale
    equity = pd.concat([pd.Series([CAPITAL]), df_t["capital"]], ignore_index=True)
    peak = equity.cummax()
    max_dd = ((equity - peak) / peak * 100).min()

    ret = (capital - CAPITAL) / CAPITAL * 100

    return {"label": label, "trades": len(df_t), "open": n_open,
            "win_rate": win_rate, "pf": pf, "max_dd": max_dd,
            "return": ret, "final_cap": capital}


# ── Strategie storiche ────────────────────────────────────────────────────────
def backtest_surfista():
    print("⏳ Backtest SURFISTA (S&P500 1H)...")
    df = get_data("^GSPC", "2y", "1h")
    if df is None: return None
    df["ema9"] = ema(df["Close"], 9); df["ema21"] = ema(df["Close"], 21)
    df["rsi"] = rsi(df["Close"], 14); df["atr"] = atr_calc(df, 14)
    df.dropna(inplace=True)
    signals = []
    for i in range(1, len(df) - 1):
        row, prev, nxt = df.iloc[i], df.iloc[i-1], df.iloc[i+1]
        h = row.name.tz_convert(ROME_TZ).hour
        if not (15 <= h <= 21): continue
        cu = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
        cd = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
        if cu and row["rsi"] > 50:
            signals.append({"entry": row["Close"], "sl": row["Close"] - 1.5 * row["atr"],
                            "tp": row["Close"] + 3.0 * row["atr"], "direction": 1,
                            "next_close": nxt["Close"]})
        elif cd and row["rsi"] < 50:
            signals.append({"entry": row["Close"], "sl": row["Close"] + 1.5 * row["atr"],
                            "tp": row["Close"] - 3.0 * row["atr"], "direction": -1,
                            "next_close": nxt["Close"]})
    if not signals: return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], "🟢 SURFISTA (S&P500 1H)")


def backtest_pendolo():
    print("⏳ Backtest IL PENDOLO (Oro 1H)...")
    df = get_data("GC=F", "2y", "1h")
    if df is None: return None
    df["rsi"] = rsi(df["Close"], 14); df["atr"] = atr_calc(df, 14)
    df["bb_mid"], df["bb_up"], df["bb_low"] = bollinger(df["Close"], 20, 2.2)
    try: df["vwap"] = calc_vwap(df)
    except: df["vwap"] = df["Close"].rolling(20).mean()
    df.dropna(inplace=True)
    signals = []
    for i in range(len(df) - 1):
        row, nxt = df.iloc[i], df.iloc[i+1]
        h = row.name.tz_convert(ROME_TZ).hour
        if not (8 <= h <= 18): continue
        if row["Close"] < row["bb_low"] and row["rsi"] < 25 and row["Close"] < row["vwap"]:
            signals.append({"entry": row["Close"], "sl": row["Close"] - 1.3 * row["atr"],
                            "tp": row["bb_mid"], "direction": 1, "next_close": nxt["Close"]})
        elif row["Close"] > row["bb_up"] and row["rsi"] > 75 and row["Close"] > row["vwap"]:
            signals.append({"entry": row["Close"], "sl": row["Close"] + 1.3 * row["atr"],
                            "tp": row["bb_mid"], "direction": -1, "next_close": nxt["Close"]})
    if not signals: return None
    return simulate(pd.DataFrame(signals), COSTS["GOLD"], "🟡 IL PENDOLO (Oro 1H)")


def backtest_rompighiaccio():
    print("⏳ Backtest ROMPIGHIACCIO (Nasdaq 15min)...")
    df = get_data("^NDX", "1y", "15m")
    if df is None: return None
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET_TZ)
    df["ema21"] = ema(df["Close"], 21); df["ema50"] = ema(df["Close"], 50)
    df["atr"] = atr_calc(df, 14)
    try: df["vwap"] = calc_vwap(df)
    except: df["vwap"] = df["Close"].rolling(20).mean()
    df.dropna(inplace=True)
    signals = []
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]
        sess = day_df[day_df.index.time >= time(9, 30)]
        if len(sess) < 4: continue
        rh = sess.iloc[:2]["High"].max(); rl = sess.iloc[:2]["Low"].min()
        for i in range(2, len(sess) - 1):
            row, nxt = sess.iloc[i], sess.iloc[i+1]
            if not (time(9, 30) <= row.name.time() <= time(11, 30)): continue
            a = row["atr"]; buf = 0.3 * a
            if row["Close"] > rh + buf and row["Close"] > row["vwap"] and row["ema21"] > row["ema50"]:
                signals.append({"entry": row["Close"], "sl": row["Close"] - 1.5 * a,
                                "tp": row["Close"] + 3.0 * a, "direction": 1,
                                "next_close": nxt["Close"]}); break
            elif row["Close"] < rl - buf and row["Close"] < row["vwap"] and row["ema21"] < row["ema50"]:
                signals.append({"entry": row["Close"], "sl": row["Close"] + 1.5 * a,
                                "tp": row["Close"] - 3.0 * a, "direction": -1,
                                "next_close": nxt["Close"]}); break
    if not signals: return None
    return simulate(pd.DataFrame(signals), COSTS["NASDAQ"], "🔵 ROMPIGHIACCIO (Nasdaq 15min)")


def backtest_barile_caldo():
    print("⏳ Backtest BARILE CALDO (WTI 4H)...")
    df_1h = get_data("CL=F", "2y", "1h")
    if df_1h is None: return None
    df = resample_4h(df_1h)
    if len(df) < 50: return None
    df["ema200"] = ema(df["Close"], 200); df["atr"] = atr_calc(df, 14)
    df["st"], df["st_dir"] = supertrend(df, 10, 3.0)
    df.dropna(inplace=True)
    signals = []
    for i in range(1, len(df) - 1):
        prev, row, nxt = df.iloc[i-1], df.iloc[i], df.iloc[i+1]
        tb = prev["st_dir"] == -1 and row["st_dir"] == 1
        ts = prev["st_dir"] == 1 and row["st_dir"] == -1
        if tb and row["Close"] > row["ema200"]:
            sl = row["st"]; risk = row["Close"] - sl
            if risk <= 0: continue
            signals.append({"entry": row["Close"], "sl": sl, "tp": row["Close"] + 2.0 * risk,
                            "direction": 1, "next_close": nxt["Close"]})
        elif ts and row["Close"] < row["ema200"]:
            sl = row["st"]; risk = sl - row["Close"]
            if risk <= 0: continue
            signals.append({"entry": row["Close"], "sl": sl, "tp": row["Close"] - 2.0 * risk,
                            "direction": -1, "next_close": nxt["Close"]})
    if not signals: return None
    return simulate(pd.DataFrame(signals), COSTS["WTI"], "🛢️ BARILE CALDO (WTI 4H)")


# ── ORB S&P500 — backtest parametrico ────────────────────────────────────────
def backtest_orb_sp500(label="ORB BASE", time_limit=time(21, 45),
                       atr_buf=0.1, vol_mult=None, rsi_long=52, rsi_short=48):
    print(f"⏳ Backtest {label}...")
    df = get_data("^GSPC", "60d", "15m")
    if df is None:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)

    df["ema20"] = ema(df["Close"], 20)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr_calc(df, 14)

    # VWAP giornaliero
    parts = []
    for d, g in df.groupby(df.index.date):
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)

    signals = []
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]
        or_bar = day_df[day_df.index.time == time(15, 30)]
        if len(or_bar) == 0:
            continue
        or_high = or_bar["High"].iloc[0]
        or_low = or_bar["Low"].iloc[0]
        or_range = or_high - or_low
        atr_day = day_df["atr14"].mean()
        if or_range > 2.5 * atr_day:
            continue

        window = day_df[(day_df.index.time >= time(15, 45)) &
                        (day_df.index.time <= time_limit)]
        traded = False
        for i in range(len(window) - 1):
            if traded:
                break
            curr = window.iloc[i]
            pos = df.index.get_loc(window.index[i])
            if pos < 1:
                continue
            prev = df.iloc[pos - 1]
            entry = curr["Close"]
            a = curr["atr14"]

            if vol_mult is not None:
                vol_avg = day_df["Volume"].iloc[:day_df.index.get_loc(window.index[i]) + 1].tail(10).mean()
                if vol_avg > 0 and curr["Volume"] < vol_mult * vol_avg:
                    continue

            sig = None
            if (entry > or_high + atr_buf * a and
                    entry > curr["vwap"] and
                    curr["ema20"] > prev["ema20"] and
                    curr["rsi14"] > rsi_long):
                sl = max(or_low, entry - 2.0 * a)
                risk = entry - sl
                if risk > 0:
                    sig = {"direction": 1, "entry": entry, "sl": sl,
                           "tp": entry + 2.5 * risk}
            elif (entry < or_low - atr_buf * a and
                  entry < curr["vwap"] and
                  curr["ema20"] < prev["ema20"] and
                  curr["rsi14"] < rsi_short):
                sl = min(or_high, entry + 2.0 * a)
                risk = sl - entry
                if risk > 0:
                    sig = {"direction": -1, "entry": entry, "sl": sl,
                           "tp": entry - 2.5 * risk}

            if sig is None:
                continue

            # Simula forward con High/Low fino a fine sessione
            future = window.iloc[i + 1:]
            exit_price = future.iloc[-1]["Close"] if len(future) > 0 else entry
            for _, fc in future.iterrows():
                if sig["direction"] == 1:
                    if fc["Low"] <= sig["sl"]:
                        exit_price = sig["sl"]; break
                    if fc["High"] >= sig["tp"]:
                        exit_price = sig["tp"]; break
                else:
                    if fc["High"] >= sig["sl"]:
                        exit_price = sig["sl"]; break
                    if fc["Low"] <= sig["tp"]:
                        exit_price = sig["tp"]; break

            sig["next_close"] = exit_price
            signals.append(sig)
            traded = True

    if not signals:
        print(f"  Nessun segnale per {label}")
        return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], f"🟢 {label}")


# ── Helper: simula forward fino a fine sessione ───────────────────────────────
def _forward_exit(future_df, direction, sl, tp):
    if len(future_df) == 0:
        return None
    for _, fc in future_df.iterrows():
        if direction == 1:
            if fc["Low"] <= sl: return sl
            if fc["High"] >= tp: return tp
        else:
            if fc["High"] >= sl: return sl
            if fc["Low"] <= tp: return tp
    return future_df.iloc[-1]["Close"]


# ── STRATEGIA INNOVATIVA 1: LIQUIDITY GRAB ────────────────────────────────────
# Concetto: le istituzioni "cacciano" la liquidità oltre l'OR per poi invertire.
# Segnale: wick che sfonda OR high/low ma la candela CHIUDE dentro l'OR.
# Logica: il fakeout è confermato → entrata opposta con SL stretto sul wick.
# ─────────────────────────────────────────────────────────────────────────────
def backtest_liquidity_grab():
    print("⏳ Backtest LIQUIDITY GRAB (S&P500 15min)...")
    df = get_data("^GSPC", "60d", "15m")
    if df is None: return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)

    df["atr14"] = atr_calc(df, 14)
    parts = []
    for d, g in df.groupby(df.index.date):
        tp_val = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp_val * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)

    signals = []
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]
        or_bar = day_df[day_df.index.time == time(15, 30)]
        if len(or_bar) == 0: continue

        or_high = or_bar["High"].iloc[0]
        or_low = or_bar["Low"].iloc[0]
        or_range = or_high - or_low
        if or_range < 2.0: continue  # OR troppo stretto = rumore di mercato

        # Finestra caccia liquidità: 15:30-17:30 (prime 2h di sessione USA)
        window = day_df[(day_df.index.time >= time(15, 30)) &
                        (day_df.index.time <= time(17, 30))]
        session_end = day_df[day_df.index.time <= time(21, 45)]

        traded = False
        for i in range(1, len(window) - 1):
            if traded: break
            curr = window.iloc[i]
            a = curr["atr14"]
            wick_min = max(0.3 * a, 2.0)  # wick minimo significativo
            or_mid = (or_high + or_low) / 2  # centro naturale dell'OR

            # GRAB RIALZISTA → segnale SHORT
            # Wick sopra OR high, chiude dentro → short verso centro OR
            if (curr["High"] > or_high + wick_min and
                    curr["Close"] < or_high and
                    curr["Close"] < curr["Open"]):
                entry = curr["Close"]
                sl = curr["High"] + 2.0  # SL oltre il wick
                risk = sl - entry
                if risk <= 0 or risk > 35: continue
                # TP = centro OR (target naturale del reversal)
                tp = or_mid if or_mid < entry else or_low
                if tp >= entry: continue
                # Filtra se R:R < 0.5 (troppo piccolo per coprire lo spread)
                if (entry - tp) < 0.5 * risk: continue
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, -1, sl, tp)
                if exit_p is None: continue
                signals.append({"direction": -1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

            # GRAB RIBASSISTA → segnale LONG
            # Wick sotto OR low, chiude dentro → long verso centro OR
            elif (curr["Low"] < or_low - wick_min and
                  curr["Close"] > or_low and
                  curr["Close"] > curr["Open"]):
                entry = curr["Close"]
                sl = curr["Low"] - 2.0  # SL oltre il wick
                risk = entry - sl
                if risk <= 0 or risk > 35: continue
                tp = or_mid if or_mid > entry else or_high
                if tp <= entry: continue
                if (tp - entry) < 0.5 * risk: continue
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, 1, sl, tp)
                if exit_p is None: continue
                signals.append({"direction": 1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

    if not signals:
        print("  Nessun segnale LIQUIDITY GRAB")
        return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], "⚡ LIQUIDITY GRAB")


# ── STRATEGIA INNOVATIVA 2: INITIAL BALANCE BREAKOUT ─────────────────────────
# Concetto: il range della prima ORA (Initial Balance, termine pro) è il
# livello più usato dai trader istituzionali. Breakout dopo 16:30 = segnale vero.
# Differenza dall'ORB: 4 candele da 15min invece di 1 → livello più robusto.
# ─────────────────────────────────────────────────────────────────────────────
def backtest_initial_balance():
    print("⏳ Backtest INITIAL BALANCE BREAKOUT (S&P500)...")
    df = get_data("^GSPC", "60d", "15m")
    if df is None: return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)

    df["ema20"] = ema(df["Close"], 20)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr_calc(df, 14)
    parts = []
    for d, g in df.groupby(df.index.date):
        tp_val = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp_val * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)

    signals = []
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]

        # Initial Balance = 15:30-16:30 CET (4 candele da 15min)
        ib = day_df[(day_df.index.time >= time(15, 30)) &
                    (day_df.index.time < time(16, 30))]
        if len(ib) < 4: continue

        ib_high = ib["High"].max()
        ib_low = ib["Low"].min()
        ib_range = ib_high - ib_low
        atr_day = day_df["atr14"].mean()

        # IB troppo largo = giorno volatile = skip
        if ib_range > 3.0 * atr_day: continue
        # IB troppo stretto = consolidamento debole = skip
        if ib_range < 0.5 * atr_day: continue

        # Finestra operativa: dopo l'IB, fino a 21:45
        window = day_df[(day_df.index.time >= time(16, 30)) &
                        (day_df.index.time <= time(21, 45))]
        session_end = day_df[day_df.index.time <= time(21, 45)]

        traded = False
        for i in range(len(window) - 1):
            if traded: break
            curr = window.iloc[i]
            pos = df.index.get_loc(window.index[i])
            if pos < 1: continue
            prev = df.iloc[pos - 1]
            entry = curr["Close"]
            a = curr["atr14"]

            # LONG: close sopra IB high + sopra VWAP + RSI > 55
            if (entry > ib_high + 0.15 * a and
                    entry > curr["vwap"] and
                    curr["rsi14"] > 55 and
                    curr["ema20"] > prev["ema20"]):
                sl = max(ib_low, entry - 2.0 * a)
                risk = entry - sl
                if risk <= 0: continue
                tp = entry + 2.5 * risk
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, 1, sl, tp)
                if exit_p is None: continue
                signals.append({"direction": 1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

            # SHORT: close sotto IB low + sotto VWAP + RSI < 45
            elif (entry < ib_low - 0.15 * a and
                  entry < curr["vwap"] and
                  curr["rsi14"] < 45 and
                  curr["ema20"] < prev["ema20"]):
                sl = min(ib_high, entry + 2.0 * a)
                risk = sl - entry
                if risk <= 0: continue
                tp = entry - 2.5 * risk
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, -1, sl, tp)
                if exit_p is None: continue
                signals.append({"direction": -1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

    if not signals:
        print("  Nessun segnale INITIAL BALANCE")
        return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], "🏛️ INITIAL BALANCE")


# ── STRATEGIA INNOVATIVA 3: GAP FILL ──────────────────────────────────────────
# Concetto: S&P500 richiude il gap rispetto alla chiusura precedente ~70% volte.
# Edge statistico puro, senza indicatori. Entra nella direzione del fill.
# SL: oltre il massimo/minimo della candela di apertura. TP: chiusura precedente.
# ─────────────────────────────────────────────────────────────────────────────
def backtest_gap_fill():
    print("⏳ Backtest GAP FILL (S&P500 15min)...")
    df = get_data("^GSPC", "60d", "15m")
    if df is None: return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)

    df["atr14"] = atr_calc(df, 14)
    df.dropna(inplace=True)

    signals = []
    days = sorted(set(df.index.date))

    for idx, d in enumerate(days[1:], 1):
        prev_day = days[idx - 1]
        day_df = df[df.index.date == d]
        prev_df = df[df.index.date == prev_day]

        # Chiusura del giorno precedente (ultima candela entro 22:00)
        prev_sess = prev_df[prev_df.index.time <= time(22, 0)]
        if len(prev_sess) == 0: continue
        prev_close = prev_sess.iloc[-1]["Close"]

        # Apertura US di oggi (candela 15:30)
        open_bar = day_df[day_df.index.time == time(15, 30)]
        if len(open_bar) == 0: continue
        today_open = open_bar["Open"].iloc[0]
        a = open_bar["atr14"].iloc[0]

        gap = today_open - prev_close
        gap_pct = abs(gap) / prev_close * 100

        # Gap significativo: almeno 0.2% e almeno 0.5*ATR
        if gap_pct < 0.2 or abs(gap) < 0.5 * a: continue
        # Gap troppo grande: oltre 1.5% = evento macro, non fare fill
        if gap_pct > 1.5: continue

        session_end = day_df[day_df.index.time <= time(21, 45)]

        # GAP UP → fill verso il basso → SHORT
        if gap > 0:
            # Aspetta conferma: candela di 15:45 chiude sotto il low di apertura
            confirm_bar = day_df[day_df.index.time == time(15, 45)]
            if len(confirm_bar) == 0: continue
            if confirm_bar.iloc[0]["Close"] >= open_bar.iloc[0]["Low"]: continue

            entry = confirm_bar.iloc[0]["Close"]
            sl = open_bar.iloc[0]["High"] + 1.0
            risk = sl - entry
            if risk <= 0 or risk > 20: continue
            tp = prev_close
            if tp >= entry: continue

            future = session_end[session_end.index > confirm_bar.index[0]]
            exit_p = _forward_exit(future, -1, sl, tp)
            if exit_p is None: continue
            signals.append({"direction": -1, "entry": entry,
                            "sl": sl, "tp": tp, "next_close": exit_p})

        # GAP DOWN → fill verso l'alto → LONG
        else:
            confirm_bar = day_df[day_df.index.time == time(15, 45)]
            if len(confirm_bar) == 0: continue
            if confirm_bar.iloc[0]["Close"] <= open_bar.iloc[0]["High"]: continue

            entry = confirm_bar.iloc[0]["Close"]
            sl = open_bar.iloc[0]["Low"] - 1.0
            risk = entry - sl
            if risk <= 0 or risk > 20: continue
            tp = prev_close
            if tp <= entry: continue

            future = session_end[session_end.index > confirm_bar.index[0]]
            exit_p = _forward_exit(future, 1, sl, tp)
            if exit_p is None: continue
            signals.append({"direction": 1, "entry": entry,
                            "sl": sl, "tp": tp, "next_close": exit_p})

    if not signals:
        print("  Nessun segnale GAP FILL")
        return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], "🔄 GAP FILL")


# ── Output (CORRETTO) ─────────────────────────────────────────────────────────
def print_results(results):
    print("\n" + "═" * 72)
    print(f" RISULTATI BACKTEST — Capitale iniziale: €{CAPITAL:.0f}")
    print("═" * 72)
    print(f"{'Strategia':<28} {'Trade':>6} {'Open':>5} {'Win%':>6} {'PF':>6} "
          f"{'MaxDD':>7} {'Rend%':>7} {'Cap.€':>8}")
    print("─" * 72)
    for r in results:
        if r is None:
            continue
        print(f"{r['label']:<28} {r['trades']:>6} {r['open']:>5} {r['win_rate']:>5.1f}% "
              f"{r['pf']:>6.2f} {r['max_dd']:>6.1f}% {r['return']:>6.1f}% "
              f"{r['final_cap']:>8.0f}€")
    print("─" * 72)
    print("\nLegenda:")
    print(" PF = profitti netti / perdite nette su tutti i trade, costi inclusi")
    print(" Open = trade chiusi senza toccare stop né target (a fine sessione; nel backtest trend: uscita per tempo)")
    print(" MaxDD = drawdown massimo % dal capitale iniziale")
    print(" Rend% = rendimento totale sul periodo testato")
    print("\n⚠️ Con pochi trade (meno di ~100) il PF non è statisticamente affidabile.")
    print("⚠️ Costi: spread in % del valore della posizione (schermata/KID Revolut). Stress test: COSTI_X=2.")
    print("⚠️ Fai sempre paper trading prima del denaro reale.\n")


# ── STRATEGIA INNOVATIVA 4: ORB + REGIME FILTER ──────────────────────────────
# Concetto: il regime di mercato determina se un breakout funzionerà.
# - VIX > 22 = mercato choppy → skip tutto
# - S&P500 sopra SMA20 giornaliera → solo LONG
# - S&P500 sotto SMA20 giornaliera → solo SHORT
# Fonti dati: ^VIX e ^GSPC daily + 15min intraday
# ─────────────────────────────────────────────────────────────────────────────
def backtest_orb_regime():
    print("⏳ Backtest ORB + REGIME FILTER (VIX + Trend)...")
    df_15m = get_data("^GSPC", "60d", "15m")
    df_day = get_data("^GSPC", "1y", "1d")
    df_vix = get_data("^VIX", "1y", "1d")

    if df_15m is None or df_day is None or df_vix is None:
        return None

    if df_15m.index.tz is None:
        df_15m.index = df_15m.index.tz_localize("UTC")
    df_15m.index = df_15m.index.tz_convert(ROME_TZ)

    df_15m["ema20"] = ema(df_15m["Close"], 20)
    df_15m["rsi14"] = rsi(df_15m["Close"], 14)
    df_15m["atr14"] = atr_calc(df_15m, 14)

    parts = []
    for d, g in df_15m.groupby(df_15m.index.date):
        tp_val = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp_val * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df_15m["vwap"] = pd.concat(parts).reindex(df_15m.index)
    df_15m.dropna(inplace=True)

    # SMA20 giornaliera S&P500
    df_day["sma20"] = df_day["Close"].rolling(20).mean()
    df_day.dropna(inplace=True)

    def get_regime(date):
        # VIX del giorno precedente
        vix_hist = df_vix[df_vix.index.date < date]
        if len(vix_hist) == 0:
            return None
        vix_val = vix_hist.iloc[-1]["Close"]
        if vix_val > 22:
            return None  # mercato troppo volatile, segnale inaffidabile

        # Trend S&P500: sopra/sotto SMA20 daily
        day_hist = df_day[df_day.index.date < date]
        if len(day_hist) == 0:
            return None
        last = day_hist.iloc[-1]
        if last["Close"] > last["sma20"]:
            return 1   # regime LONG
        else:
            return -1  # regime SHORT

    signals = []
    for d in sorted(set(df_15m.index.date)):
        regime = get_regime(d)
        if regime is None:
            continue  # VIX alto o dati insufficienti

        day_df = df_15m[df_15m.index.date == d]
        or_bar = day_df[day_df.index.time == time(15, 30)]
        if len(or_bar) == 0:
            continue

        or_high = or_bar["High"].iloc[0]
        or_low = or_bar["Low"].iloc[0]
        or_range = or_high - or_low
        atr_day = day_df["atr14"].mean()
        if or_range > 2.5 * atr_day:
            continue

        window = day_df[(day_df.index.time >= time(15, 45)) &
                        (day_df.index.time <= time(21, 45))]
        session_end = day_df[day_df.index.time <= time(21, 45)]

        traded = False
        for i in range(len(window) - 1):
            if traded:
                break
            curr = window.iloc[i]
            pos = df_15m.index.get_loc(window.index[i])
            if pos < 1:
                continue
            prev = df_15m.iloc[pos - 1]
            entry = curr["Close"]
            a = curr["atr14"]

            if (regime == 1 and
                    entry > or_high + 0.1 * a and
                    entry > curr["vwap"] and
                    curr["ema20"] > prev["ema20"] and
                    curr["rsi14"] > 52):
                sl = max(or_low, entry - 2.0 * a)
                risk = entry - sl
                if risk <= 0:
                    continue
                tp = entry + 2.5 * risk
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, 1, sl, tp)
                if exit_p is None:
                    continue
                signals.append({"direction": 1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

            elif (regime == -1 and
                  entry < or_low - 0.1 * a and
                  entry < curr["vwap"] and
                  curr["ema20"] < prev["ema20"] and
                  curr["rsi14"] < 48):
                sl = min(or_high, entry + 2.0 * a)
                risk = sl - entry
                if risk <= 0:
                    continue
                tp = entry - 2.5 * risk
                future = session_end[session_end.index > window.index[i]]
                exit_p = _forward_exit(future, -1, sl, tp)
                if exit_p is None:
                    continue
                signals.append({"direction": -1, "entry": entry,
                                "sl": sl, "tp": tp, "next_close": exit_p})
                traded = True

    if not signals:
        print("  Nessun segnale ORB REGIME")
        return None
    return simulate(pd.DataFrame(signals), COSTS["SP500"], "🧭 ORB + REGIME")


# ── ORB su azioni singole (stessa logica del bot live) ───────────────────────
# Per CAT e AVGO. Le condizioni replicano check_orb_azione() di strategies.py.
# Differenze: un solo trade al giorno; nessun TP1/break-even; spread in % del valore
# della posizione (KID azioni). ATR giornaliero calcolato solo sulle candele già
# avvenute (nessun lookahead).
def backtest_orb_azione(ticker, label, max_risk_pct=0.05, permetti_short=True):
    print(f"⏳ Backtest {label}...")
    df = get_data(ticker, "60d", "15m")
    if df is None:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)

    df["ema20"] = ema(df["Close"], 20)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr_calc(df, 14)
    parts = []
    for d, g in df.groupby(df.index.date):
        tp_val = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp_val * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df.dropna(inplace=True)

    signals = []
    for d in sorted(set(df.index.date)):
        day_df = df[df.index.date == d]
        or_bar = day_df[day_df.index.time == time(15, 30)]
        if len(or_bar) == 0:
            continue
        or_high = or_bar["High"].iloc[0]
        or_low = or_bar["Low"].iloc[0]
        or_range = or_high - or_low
        session = day_df[day_df.index.time <= time(21, 45)]

        # Candele valutabili: il bot le legge tra le 16:00 e le 20:30,
        # cioè candele con inizio tra 15:45 e 20:15
        for i in range(1, len(day_df)):
            curr = day_df.iloc[i]
            prev = day_df.iloc[i - 1]
            t = curr.name.time()
            if not (time(15, 45) <= t <= time(20, 15)):
                continue

            atr_day = day_df["atr14"].iloc[:i + 1].mean()   # solo dati già noti
            if or_range > 3.0 * atr_day:
                break
            entry = curr["Close"]
            a = curr["atr14"]
            sig = None

            if (entry > or_high + 0.1 * a and entry > curr["vwap"] and
                    curr["ema20"] > prev["ema20"] and curr["rsi14"] > 52):
                sl = max(or_low, entry - 2.0 * a)
                risk = entry - sl
                if risk > 0 and risk / entry <= max_risk_pct:
                    sig = {"direction": 1, "entry": entry, "sl": sl, "tp": entry + 2.5 * risk}
            elif (permetti_short and entry < or_low - 0.1 * a and entry < curr["vwap"] and
                  curr["ema20"] < prev["ema20"] and curr["rsi14"] < 48):
                sl = min(or_high, entry + 2.0 * a)
                risk = sl - entry
                if risk > 0 and risk / entry <= max_risk_pct:
                    sig = {"direction": -1, "entry": entry, "sl": sl, "tp": entry - 2.5 * risk}

            if sig is None:
                continue
            future = session[session.index > curr.name]
            exit_p = _forward_exit(future, sig["direction"], sig["sl"], sig["tp"])
            if exit_p is None:
                continue
            sig["next_close"] = exit_p
            signals.append(sig)
            break   # un solo trade al giorno

    if not signals:
        print(f"  Nessun segnale per {label}")
        return None
    return simulate(pd.DataFrame(signals), COSTS["AZIONI"], f"🏭 {label}")


# ═════════════════════════════════════════════════════════════════════════════
# RITRACCIAMENTO NEL TREND — S&P500 GIORNALIERO (RSI a 2 giorni)
#
# Idea classica di "mean reversion nel trend": nei mercati rialzisti i cali brevi
# e violenti tendono a rientrare in pochi giorni.
#   Trend:    chiusura sopra la media a 200 giorni
#   Ingresso: RSI(2) < soglia (default 10) alla chiusura → compra all'APERTURA
#             del giorno dopo (nessun lookahead, c'è il rischio gap)
#   Uscita:   chiusura sopra la media a 5 giorni | stop = 3 × ATR(14) | max 10 giorni
#   Solo long. Rischio per trade = RISK_PCT del capitale (size dallo stop).
#
# ⚠️ Costi: vedi le costanti qui sotto (derivate dal KID Revolut dei CFD su indici).
# ⚠️ Non ho risultati reali: lancia `python3 backtest.py trend` e valuta tu.
# ═════════════════════════════════════════════════════════════════════════════
# Costi Revolut — fonti: schermata ordine CFD S&P 500 e KID CFD indici / azioni / materie prime (28/04/2026)
#  Spread andata+ritorno, % del valore della posizione (il KID lo conta una volta per apri+chiudi):
#   - S&P 500 (schermata): 0,27$ su 1.441,89$ di esposizione ≈ 0,0187%
#   - KID esempio: indici 0,0042% | azioni 0,0394% | materie prime 0,0375%
#  Commissione notturna LONG = posizione × (tasso base + mark-up Revolut 3%) / 365:
#   - S&P 500 (schermata): 0,27$ su 1.441,89$ al giorno ≈ 0,0187% ≈ 6,8% annuo → tasso base ≈ 3,8%
#   - Oro (schermata XAU:CFD): 0,26$ su 1.785,82$ al giorno ≈ 0,0146% ≈ 5,3% annuo (margine 5%)
#   - KID esempio: indici/azioni 0,0182% al giorno ≈ 6,64% | materie prime 0,0164% ≈ 6,0%
#   - Il weekend è coperto triplicando la commissione una volta a settimana: equivale ai giorni
#     di calendario, come calcolato qui. Gli short ricevono circa tasso base − 3% (non usato: solo long).
# ⚠️ Spread e tasso variano per strumento, orario e nel tempo: ri-controlla nell'app.
TASSO_BASE_ANNUO = 0.038
MARKUP_REVOLUT = 0.03
COSTO_SPREAD_PCT = COSTS["SP500"]                          # ≈0,02% andata e ritorno
FINANZIAMENTO_ANNUO = TASSO_BASE_ANNUO + MARKUP_REVOLUT   # ≈ 6,8% annuo per posizioni LONG


def _trades_pullback(df, rsi_soglia=10, usa_filtro_trend=True, atr_stop=3.0, max_giorni=10,
                     uscita_apertura_dopo=False):
    d = df[["Open", "High", "Low", "Close"]].copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["sma5"] = d["Close"].rolling(5).mean()
    d["rsi2"] = rsi(d["Close"], 2)
    d["atr"] = atr_calc(d, 14)
    d.dropna(inplace=True)

    O, L, C = d["Open"].values, d["Low"].values, d["Close"].values
    S5, S200, R2, A = d["sma5"].values, d["sma200"].values, d["rsi2"].values, d["atr"].values
    dates = d.index
    n = len(d)

    trades = []
    i = 0
    while i < n - 1:
        trend_ok = (C[i] > S200[i]) if usa_filtro_trend else True
        if trend_ok and R2[i] < rsi_soglia:
            e = i + 1                          # ingresso all'apertura del giorno dopo
            entry = O[e]
            stop = entry - atr_stop * A[i]
            exit_j, ex, motivo = None, None, None
            for j in range(e, n):
                if O[j] <= stop:               # gap sotto lo stop: esce all'apertura
                    ex, motivo = O[j], "stop"
                elif L[j] <= stop:             # stop toccato durante il giorno
                    ex, motivo = stop, "stop"
                elif C[j] > S5[j]:             # rientro: chiusura sopra la media a 5 giorni
                    ex, motivo = C[j], "uscita"
                elif (j - e + 1) >= max_giorni:
                    ex, motivo = C[j], "tempo"
                else:
                    continue
                exit_j = j
                break
            if exit_j is None:
                exit_j, ex, motivo = n - 1, C[n - 1], "fine dati"
            elif uscita_apertura_dopo and motivo in ("uscita", "tempo") and exit_j + 1 < n:
                # In live il segnale di uscita arriva a mercato chiuso: si esce all'apertura dopo
                exit_j, ex = exit_j + 1, O[exit_j + 1]
            trades.append({"entry_date": dates[e], "exit_date": dates[exit_j],
                           "entry": entry, "stop": stop, "exit": ex, "motivo": motivo,
                           "giorni": exit_j - e + 1})
            i = exit_j + 1
        else:
            i += 1
    return trades, dates[0], dates[-1], C[0], C[-1]


# Azioni singole in CFD: spread ≈ 0,0394% (KID azioni, esempio a 254$); finanziamento come per gli indici.
COSTI_AZIONI = {"spread": COSTS["AZIONI"], "finanz": FINANZIAMENTO_ANNUO}


def _metriche_trades(trades, label, costo_spread=None, finanz=None):
    costo_spread = COSTO_SPREAD_PCT if costo_spread is None else costo_spread
    finanz = FINANZIAMENTO_ANNUO if finanz is None else finanz
    if not trades:
        return None
    capital = CAPITAL
    pnl_list, caps, giorni, stop_n, tempo_n = [], [], [], 0, 0
    for t in trades:
        dist = t["entry"] - t["stop"]
        if dist <= 0:
            continue
        size = capital * RISK_PCT / dist
        giorni_cal = max((t["exit_date"] - t["entry_date"]).days, 0)
        costo = size * t["entry"] * (costo_spread + finanz * giorni_cal / 365)
        pnl = (t["exit"] - t["entry"]) * size - costo
        capital += pnl
        pnl_list.append(pnl); caps.append(capital); giorni.append(t["giorni"])
        stop_n += t["motivo"] == "stop"
        tempo_n += t["motivo"] == "tempo"
    if not pnl_list:
        return None
    s = pd.Series(pnl_list)
    gross_p = s[s > 0].sum()
    gross_l = abs(s[s <= 0].sum())
    equity = pd.concat([pd.Series([CAPITAL]), pd.Series(caps)], ignore_index=True)
    peak = equity.cummax()
    return {"label": label, "trades": len(s), "open": int(tempo_n),
            "win_rate": (s > 0).mean() * 100,
            "pf": gross_p / gross_l if gross_l > 0 else float("inf"),
            "max_dd": ((equity - peak) / peak * 100).min(),
            "return": (capital - CAPITAL) / CAPITAL * 100, "final_cap": capital,
            "giorni_medi": sum(giorni) / len(giorni), "stop": int(stop_n)}


def backtest_pullback(ticker, nome, start="1993-01-01", costo_spread=None, finanz=None):
    """Ritracciamento nel trend su qualsiasi titolo/indice giornaliero."""
    print(f"⏳ Backtest RITRACCIAMENTO NEL TREND ({nome} giornaliero)...")
    df = get_data(ticker, "max", "1d")
    if df is None:
        return None
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df[df.index >= start]

    trades, d0, d1, c0, c1 = _trades_pullback(df)
    if not trades:
        print("  Nessun trade")
        return None

    cs = COSTO_SPREAD_PCT if costo_spread is None else costo_spread
    fz = FINANZIAMENTO_ANNUO if finanz is None else finanz

    # Dati "da parte": primo 70% del periodo = sviluppo, ultimo 30% = verifica
    taglio = d0 + (d1 - d0) * 0.7
    ins = [t for t in trades if t["entry_date"] < taglio]
    oos = [t for t in trades if t["entry_date"] >= taglio]

    def M(tr, lab, k=1.0):
        return _metriche_trades(tr, lab, cs * k, fz * k)

    risultati = [
        M(ins, "Sviluppo (70%) RSI2<10"),
        M(oos, "VERIFICA (30%) RSI2<10"),
        M(_trades_pullback(df, rsi_soglia=5)[0], "Soglia 5 (tutto)"),
        M(_trades_pullback(df, rsi_soglia=15)[0], "Soglia 15 (tutto)"),
        M(_trades_pullback(df, usa_filtro_trend=False)[0], "Senza filtro SMA200"),
        M(trades, "Tutto, costi x2", 2.0),
        M(trades, "Tutto, costi x3", 3.0),
        M(_trades_pullback(df, uscita_apertura_dopo=True)[0], "Uscita apertura dopo (live)"),
    ]
    risultati = [r for r in risultati if r is not None]

    print(f"\n━━━━━━━━━━━━ {nome} ({ticker}) ━━━━━━━━━━━━")
    print_results(risultati)
    print(f"Periodo: {d0:%Y-%m-%d} → {d1:%Y-%m-%d}  |  Taglio sviluppo/verifica: {taglio:%Y-%m-%d}")
    print(f"Costi usati: spread {cs * 100:.3f}% a trade + finanziamento {fz * 100:.1f}% annuo (da schermata/KID Revolut: ri-controlla nell'app)")
    print(f"Buy & hold sullo stesso periodo: {(c1 / c0 - 1) * 100:+.0f}% (senza leva né costi)")
    for r in risultati:
        print(f" {r['label']:<28} giorni medi {r['giorni_medi']:.1f} | uscite per stop {r['stop']} | per tempo {r['open']}")
    print("\nCome leggere: conta la riga VERIFICA. Le righe soglia 5/15 e 'senza filtro' mostrano")
    print("se il risultato è stabile; le righe 'costi x2/x3' mostrano quanto regge se i costi")
    print("reali sono più alti del previsto. Se il PF scende sotto ~1.1 con costi x2, è fragile.")
    print("Rischio 1% per trade; i gap sui risultati trimestrali sono nei dati ma NON evitati.\n")
    return risultati


def backtest_pullback_sp500():
    return backtest_pullback("^GSPC", "S&P 500")


def backtest_pullback_azioni():
    return [
        backtest_pullback("CAT", "Caterpillar", costo_spread=COSTI_AZIONI["spread"], finanz=COSTI_AZIONI["finanz"]),
        backtest_pullback("AVGO", "Broadcom", costo_spread=COSTI_AZIONI["spread"], finanz=COSTI_AZIONI["finanz"]),
    ]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "orb":
        print("🔍 Ottimizzazione ORB S&P500 — 60 giorni dati...\n")
        results = [
            backtest_orb_sp500("ORB BASE (attuale)"),
            backtest_liquidity_grab(),
            backtest_initial_balance(),
            backtest_gap_fill(),
            backtest_orb_regime(),
        ]
        print_results([r for r in results if r is not None])
    elif len(sys.argv) > 1 and sys.argv[1] == "trend":
        backtest_pullback_sp500()
    elif len(sys.argv) > 1 and sys.argv[1] == "trend_azioni":
        backtest_pullback_azioni()
    elif len(sys.argv) > 1 and sys.argv[1] == "azioni":
        print("🔍 Backtest ORB su azioni singole — circa 60 giorni di dati...\n")
        results = [
            backtest_orb_azione("CAT", "ORB CAT", max_risk_pct=0.03),
            backtest_orb_azione("AVGO", "ORB AVGO", max_risk_pct=0.04),
        ]
        print_results([r for r in results if r is not None])
    else:
        print("🔍 Avvio backtest su dati storici reali...")
        print("   (può richiedere 2-5 minuti)\n")
        results = [backtest_surfista(), backtest_pendolo(),
                   backtest_rompighiaccio(), backtest_barile_caldo()]
        print_results([r for r in results if r is not None])
