"""
BACKTEST — Simulazione storica delle strategie
Esegui con: python3 backtest.py          (4 strategie storiche)
            python3 backtest.py orb      (confronto varianti ORB S&P500)
            python3 backtest.py azioni   (ORB su CAT e AVGO)
            python3 backtest.py trend    (ritracciamento nel trend, S&P500 giornaliero)
            python3 backtest.py trend_azioni   (stessa idea su CAT e AVGO)
            python3 backtest.py bot      (VWAP Reversal, London Sweep, Kumo Oro e Nasdaq)
            python3 backtest.py nuove    (idee non nel bot: trend following oro, fine mese S&P500)

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
    df = df.dropna()
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
    df = df.dropna()
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
    df = df.dropna()
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
    df = df.dropna()
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
def backtest_orb_sp500(label="ORB BASE", atr_buf=0.1, vol_mult=None, rsi_long=52, rsi_short=48):
    """ORB S&P500 con le stesse regole e gli stessi orari del bot (strategies.check_orb_sp500):
     - opening range = candela delle 09:30 di New York (non 15:30 italiane: corretto nelle
       settimane con ora legale sfasata);
     - candele valutate 09:45–15:15 ET: il bot controlla ai :02/:17/:32/:47 tra 09:45 e
       15:45 ET, l'ultimo controllo (15:32) legge la candela delle 15:15;
     - filtro ampiezza OR con l'ATR medio delle sole candele già disponibili (niente lookahead);
     - pausa come main.should_send: stessa direzione non riproposta per 8 ore, quindi può
       esserci un LONG e uno SHORT nello stesso giorno;
     - uscita: stop o target, altrimenti chiusura alle 15:45 ET (ora indicata nel messaggio).
    TP1 e break-even non simulati."""
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
    giorni_et = df.index.tz_convert(ET_TZ).date
    parts = []
    for d in pd.unique(giorni_et):                      # VWAP per giorno di borsa USA
        g = df[giorni_et == d]
        tp = (g["High"] + g["Low"] + g["Close"]) / 3
        parts.append((tp * g["Volume"]).cumsum() / g["Volume"].cumsum())
    df["vwap"] = pd.concat(parts).reindex(df.index)
    df = df.dropna()

    idx_et = df.index.tz_convert(ET_TZ)
    signals, ultimo = [], None
    for d in pd.unique(idx_et.date):
        day = df[idx_et.date == d]
        t_et = day.index.tz_convert(ET_TZ).time
        or_bar = day[t_et == time(9, 30)]
        if len(or_bar) == 0:
            continue
        or_high, or_low = or_bar["High"].iloc[0], or_bar["Low"].iloc[0]
        or_range = or_high - or_low

        # Chiusura intraday: 15:45 ET, o 15 minuti prima della chiusura nelle mezze sedute
        # (come il bot). Ultima candela valutata: 30 minuti prima (15:15, o 12:15).
        try:
            from news_filter import MEZZE_SEDUTE_USA
            mezza = MEZZE_SEDUTE_USA.get(d)
        except Exception:
            mezza = None
        chiusura = (pd.Timestamp.combine(d, mezza) - pd.Timedelta(minutes=15)).time() if mezza else time(15, 45)
        ultima = (pd.Timestamp.combine(d, chiusura) - pd.Timedelta(minutes=30)).time()
        for k in range(len(day) - 1):                  # serve la candela dopo (in formazione)
            if not (time(9, 45) <= t_et[k] <= ultima):
                continue
            # il bot fa la media sulle candele di oggi disponibili, compresa quella in formazione
            atr_day = day["atr14"].iloc[:k + 2].mean()
            if or_range > 2.5 * atr_day:
                continue
            curr = day.iloc[k]
            pos = df.index.get_loc(day.index[k])
            if pos < 1:
                continue
            prev = df.iloc[pos - 1]
            entry, a = curr["Close"], curr["atr14"]

            if vol_mult is not None:
                vol_avg = day["Volume"].iloc[:k + 1].tail(10).mean()
                if vol_avg > 0 and curr["Volume"] < vol_mult * vol_avg:
                    continue

            sig = None
            if (entry > or_high + atr_buf * a and entry > curr["vwap"] and
                    curr["ema20"] > prev["ema20"] and curr["rsi14"] > rsi_long):
                sl = max(or_low, entry - 2.0 * a)
                if entry - sl > 0:
                    sig = {"direction": 1, "entry": entry, "sl": sl, "tp": entry + 2.5 * (entry - sl)}
            elif (entry < or_low - atr_buf * a and entry < curr["vwap"] and
                  curr["ema20"] < prev["ema20"] and curr["rsi14"] < rsi_short):
                sl = min(or_high, entry + 2.0 * a)
                if sl - entry > 0:
                    sig = {"direction": -1, "entry": entry, "sl": sl, "tp": entry - 2.5 * (sl - entry)}
            if sig is None:
                continue
            ts = day.index[k]
            if ultimo and ultimo[0] == sig["direction"] and ts - ultimo[1] < pd.Timedelta(hours=8):
                continue
            dopo = day.iloc[k + 1:]
            dopo = dopo[dopo.index.tz_convert(ET_TZ).time < chiusura]
            uscita = _forward_exit(dopo, sig["direction"], sig["sl"], sig["tp"])
            if uscita is None:
                continue
            ultimo = (sig["direction"], ts)
            sig["next_close"], sig["ts"] = uscita, ts
            signals.append(sig)

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
    df = df.dropna()

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
    df = df.dropna()

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
    df = df.dropna()

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
    df_15m = df_15m.dropna()

    # SMA20 giornaliera S&P500
    df_day["sma20"] = df_day["Close"].rolling(20).mean()
    df_day = df_day.dropna()

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
    df = df.dropna()

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
    d = d.dropna()

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
            differita = False
            if exit_j is None:
                exit_j, ex, motivo = n - 1, C[n - 1], "fine dati"
            elif uscita_apertura_dopo and motivo in ("uscita", "tempo") and exit_j + 1 < n:
                # In live il segnale di uscita arriva a mercato chiuso: si esce all'apertura dopo
                exit_j, ex, differita = exit_j + 1, O[exit_j + 1], True
            trades.append({"entry_date": dates[e], "exit_date": dates[exit_j],
                           "entry": entry, "stop": stop, "exit": ex, "motivo": motivo,
                           "giorni": exit_j - e + 1, "signal_date": dates[i]})
            # Prossimo segnale: dal giorno dopo l'evento di uscita. Con l'uscita all'apertura
            # del giorno dopo, quel giorno stesso può già dare un nuovo segnale in chiusura
            # (come fa il bot).
            i = exit_j if differita else exit_j + 1
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


# ═════════════════════════════════════════════════════════════════════════════
# STRATEGIE DEL BOT 1, 2, 4, 5 — VWAP Reversal Oro, London Sweep Oro,
# Kumo Rider Oro 4H, Kumo Rider Nasdaq 1H
# Esegui con: python3 backtest.py bot
#
# Stesse regole di strategies.py, valutate candela per candela solo con i dati
# già chiusi (nessun lookahead). Uscita: stop o target toccati sulle candele
# successive; altrimenti chiusura forzata (intraday alle 21:45 italiane, swing
# dopo un numero massimo di candele) → colonna "Open".
# Pausa tra segnali nella stessa direzione come nel bot (4h VWAP, 12h London e
# Kumo Oro, 8h Kumo Nasdaq).
#
# Semplificazioni (vedi anche i limiti nel README):
#  - prezzi del future COMEX (GC=F) per l'oro, come il bot;
#  - London Sweep: tutta la posizione a target o stop (il bot suggerisce TP1 al 50%
#    e trailing di $4, non simulati);
#  - Kumo: chikou = chiusura attuale > chiusura di 26 candele prima (regola
#    Ichimoku standard); swing senza commissione notturna;
#  - filtro notizie non applicato;
#  - se nella stessa candela si toccano stop e target, conta lo stop (prudente).
# ⚠️ Dati Yahoo: 15 minuti solo per ~60 giorni → VWAP e London Sweep hanno pochi trade.
# ═════════════════════════════════════════════════════════════════════════════
def _indice_roma(df):
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME_TZ)
    return df


def _pausa_ok(ultimi, direzione, ts, ore):
    """Pausa del bot: stessa direzione non riproposta prima di `ore` ore."""
    prec = ultimi.get(direzione)
    return prec is None or (ts - prec) >= pd.Timedelta(hours=ore)


def _uscita_intraday(df, i, direzione, sl, tp, fine=time(21, 45)):
    """Candele dopo la i-esima, fino alle 21:45 italiane dello stesso giorno."""
    giorno = df.index[i].date()
    dopo = df.iloc[i + 1:]
    dopo = dopo[(dopo.index.date == giorno) & (dopo.index.time < fine)]
    return _forward_exit(dopo, direzione, sl, tp)


def _uscita_swing(df, i, direzione, sl, tp, max_candele):
    return _forward_exit(df.iloc[i + 1:i + 1 + max_candele], direzione, sl, tp)


def backtest_vwap_reversal_oro():
    print("⏳ Backtest VWAP REVERSAL ORO (15min, ~60 giorni)...")
    df = get_data("GC=F", "60d", "15m")
    if df is None:
        return None
    df = _indice_roma(df)
    df["rsi14"] = rsi(df["Close"], 14)
    df["atr14"] = atr_calc(df, 14)
    giorni = df.index.date
    vw, up, dn = [], [], []
    for d in pd.unique(giorni):
        g = df[giorni == d]
        tp_ = (g["High"] + g["Low"] + g["Close"]) / 3
        v = (tp_ * g["Volume"]).cumsum() / g["Volume"].cumsum()
        sd = np.sqrt(((tp_ - v) ** 2 * g["Volume"]).cumsum() / g["Volume"].cumsum())
        vw.append(v); up.append(v + 2 * sd); dn.append(v - 2 * sd)
    df["vwap"] = pd.concat(vw).reindex(df.index)
    df["vb2u"] = pd.concat(up).reindex(df.index)
    df["vb2d"] = pd.concat(dn).reindex(df.index)
    df = df.dropna()

    signals, ultimi = [], {}
    for i in range(1, len(df) - 1):
        curr, prev = df.iloc[i], df.iloc[i - 1]
        ts = df.index[i]
        if not (time(8, 0) <= ts.time() <= time(21, 15)):    # come il bot
            continue
        entry, a = curr["Close"], curr["atr14"]
        sig = None
        if entry < curr["vb2d"] and curr["rsi14"] < 35 and curr["Close"] > prev["Close"]:
            sl, tp = entry - 1.2 * a, curr["vwap"]
            if (tp - entry) >= 1.5 * (entry - sl):
                sig = (1, sl, tp)
        elif entry > curr["vb2u"] and curr["rsi14"] > 65 and curr["Close"] < prev["Close"]:
            sl, tp = entry + 1.2 * a, curr["vwap"]
            if (entry - tp) >= 1.5 * (sl - entry):
                sig = (-1, sl, tp)
        if sig is None or not _pausa_ok(ultimi, sig[0], ts, 4):
            continue
        uscita = _uscita_intraday(df, i, sig[0], sig[1], sig[2])
        if uscita is None:
            continue
        ultimi[sig[0]] = ts
        signals.append({"entry": entry, "sl": sig[1], "tp": sig[2], "direction": sig[0],
                        "next_close": uscita, "ts": ts})
    if not signals:
        print("  Nessun segnale")
        return None
    return simulate(pd.DataFrame(signals), COSTS["GOLD"], "🟡 VWAP REVERSAL ORO")


def backtest_london_sweep_oro():
    print("⏳ Backtest LONDON SWEEP ORO (15min, ~60 giorni)...")
    df = get_data("GC=F", "60d", "15m")
    if df is None:
        return None
    df = _indice_roma(df)
    df["sma20"] = df["Close"].rolling(20).mean()
    df["atr14"] = atr_calc(df, 14)
    df = df.dropna()

    signals = []
    for d in sorted(set(df.index.date)):
        oggi = df[df.index.date == d]
        asia = oggi[(oggi.index.time >= time(6, 0)) & (oggi.index.time < time(8, 0))]
        if len(asia) < 3:
            continue
        asia_low, asia_high = asia["Low"].min(), asia["High"].max()
        # Il bot valuta le candele chiuse con inizio 08:15–09:30: controlla ogni 15 minuti
        # tra 08:00 e 10:00, la 08:00 non è mai l'ultima chiusa con 3 candele in finestra,
        # e la 09:45 si chiude dopo le 10:00, quando il controllo è già fuori finestra.
        for ts in oggi.index:
            if not (time(8, 15) <= ts.time() <= time(9, 30)):
                continue
            i = df.index.get_loc(ts)
            if i < 4:
                continue
            curr = df.iloc[i]
            depth = asia_low - curr["Low"]
            if not (2.0 <= depth <= 5.0) or curr["Close"] <= asia_low:
                continue
            if df["sma20"].iloc[i] - df["sma20"].iloc[i - 3] > 0.5:
                continue
            entry = curr["Close"]
            sl, tp = curr["Low"] - 3.0, asia_high
            risk = entry - sl
            if risk <= 0 or risk > 15:
                continue
            uscita = _uscita_intraday(df, i, 1, sl, tp)
            if uscita is None:
                continue
            signals.append({"entry": entry, "sl": sl, "tp": tp, "direction": 1,
                            "next_close": uscita, "ts": ts})
            break      # pausa 12h: al massimo un segnale al giorno
    if not signals:
        print("  Nessun segnale")
        return None
    return simulate(pd.DataFrame(signals), COSTS["GOLD"], "🟡 LONDON SWEEP ORO")


def _segnali_kumo(df, filtro_orario=None, pausa_ore=12, max_candele=60):
    """Kumo Rider: incrocio Tenkan/Kijun + prezzo oltre la nuvola + RSI + chikou.
    df già con indice italiano. filtro_orario(ts) -> bool sulle candele valutabili."""
    h, l, c = df["High"], df["Low"], df["Close"]
    df = df.copy()
    df["t_sen"] = (h.rolling(9).max() + l.rolling(9).min()) / 2
    df["k_sen"] = (h.rolling(26).max() + l.rolling(26).min()) / 2
    df["s_a"] = ((df["t_sen"] + df["k_sen"]) / 2).shift(26)
    df["s_b"] = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    df["c_26"] = c.shift(26)              # chikou: chiusura di 26 candele prima
    df["rsi14"] = rsi(c, 14)
    df["atr14"] = atr_calc(df, 14)
    df = df.dropna()

    signals, ultimi = [], {}
    for i in range(1, len(df) - 1):
        curr, prev = df.iloc[i], df.iloc[i - 1]
        ts = df.index[i]
        if filtro_orario and not filtro_orario(ts):
            continue
        top, bot = max(curr["s_a"], curr["s_b"]), min(curr["s_a"], curr["s_b"])
        up = prev["t_sen"] <= prev["k_sen"] and curr["t_sen"] > curr["k_sen"]
        dn = prev["t_sen"] >= prev["k_sen"] and curr["t_sen"] < curr["k_sen"]
        entry, a = curr["Close"], curr["atr14"]
        sig = None
        if (up and entry > top and 40 < curr["rsi14"] < 65 and entry > curr["c_26"]
                and curr["t_sen"] > curr["s_a"] and curr["t_sen"] > curr["s_b"]):
            sl = curr["k_sen"] - 0.3 * a
            if entry - sl > 0:
                sig = (1, sl, entry + 2.0 * (entry - sl))
        elif (dn and entry < bot and 35 < curr["rsi14"] < 60 and entry < curr["c_26"]
                and curr["t_sen"] < curr["s_a"] and curr["t_sen"] < curr["s_b"]):
            sl = curr["k_sen"] + 0.3 * a
            if sl - entry > 0:
                sig = (-1, sl, entry - 2.0 * (sl - entry))
        if sig is None or not _pausa_ok(ultimi, sig[0], ts, pausa_ore):
            continue
        uscita = _uscita_swing(df, i, sig[0], sig[1], sig[2], max_candele)
        if uscita is None:
            continue
        ultimi[sig[0]] = ts
        signals.append({"entry": entry, "sl": sig[1], "tp": sig[2], "direction": sig[0],
                        "next_close": uscita, "ts": ts})
    return signals


def backtest_kumo_oro_4h():
    print("⏳ Backtest KUMO RIDER ORO 4H (~2 anni)...")
    df_1h = get_data("GC=F", "2y", "1h")
    if df_1h is None:
        return None
    df = resample_4h(_indice_roma(df_1h))
    # uscita forzata dopo 60 candele 4H (~10 giorni di borsa)
    signals = _segnali_kumo(df, pausa_ore=12, max_candele=60)
    if not signals:
        print("  Nessun segnale")
        return None
    return simulate(pd.DataFrame(signals), COSTS["GOLD"], "🟡 KUMO RIDER ORO 4H")


def backtest_kumo_nasdaq_1h():
    print("⏳ Backtest KUMO RIDER NASDAQ 1H (~2 anni)...")
    df = get_data("^NDX", "2y", "1h")
    if df is None:
        return None
    df = _indice_roma(df)

    def orario(ts):
        # Il bot controlla alle :32 di ogni ora fino alle 15:35 di New York, subito dopo la
        # chiusura delle candele orarie (:30): valuta le candele con inizio 09:30–14:30.
        t = ts.tz_convert(ET_TZ).time()
        return time(9, 30) <= t <= time(15, 0)
    # uscita forzata dopo 70 candele 1H (~10 sedute)
    signals = _segnali_kumo(df, filtro_orario=orario, pausa_ore=8, max_candele=70)
    if not signals:
        print("  Nessun segnale")
        return None
    return simulate(pd.DataFrame(signals), COSTS["NASDAQ"], "🔵 KUMO RIDER NASDAQ 1H")


# ═════════════════════════════════════════════════════════════════════════════
# NUOVE IDEE (non nel bot) — python3 backtest.py nuove
#  A. Trend following giornaliero sull'oro (e, per robustezza, su altri mercati)
#     A1. Breakout di Donchian 55/20 ("Turtle"): LONG se la chiusura supera il massimo
#         delle 55 chiusure precedenti, esce se scende sotto il minimo delle 20
#         precedenti; SHORT speculare. Stop iniziale 2×ATR(20). Ordini all'apertura dopo.
#     A2. Medie 50/200: LONG finché la media a 50 giorni è sopra quella a 200
#         (variante long/short: SHORT quando è sotto). Ordini all'apertura dopo.
#  B. Inizio/fine mese sull'S&P500: compra all'APERTURA dell'ultimo giorno di borsa del
#     mese, vende alla CHIUSURA del 3° giorno di borsa del mese dopo. Stop 3×ATR(14).
# Regole e parametri FISSATI PRIMA del test (valori classici), nessuna ottimizzazione.
# Size: rischio RISK_PCT del capitale sulla distanza dello stop (A2: 2×ATR teorico).
# Costi: spread a trade + finanziamento notturno per giorno di calendario, anche sugli
# SHORT (prudente: su Revolut lo short può costare meno o incassare).
# Dati facoltativi Stooq (1985-2021) per l'oro: STOOQ_CSV=/percorso/file.csv
# ═════════════════════════════════════════════════════════════════════════════
FINANZIAMENTO_ORO = 0.053        # ≈5,3% annuo (schermata XAU:CFD, vedi sopra)


def _metriche_dir(trades, label, spread, finanz):
    """Come _metriche_trades ma con direzione (+1 long, -1 short) e distanza di rischio."""
    capital, pnl, caps, giorni = CAPITAL, [], [], []
    for t in trades:
        if t["rischio"] <= 0:
            continue
        size = capital * RISK_PCT / t["rischio"]
        gg = max((t["exit_date"] - t["entry_date"]).days, 0)
        costo = size * t["entry"] * (spread * MOLTIPLICATORE_COSTI + finanz * MOLTIPLICATORE_COSTI * gg / 365)
        p = (t["exit"] - t["entry"]) * t["dir"] * size - costo
        capital += p
        pnl.append(p); caps.append(capital); giorni.append(gg)
    if not pnl:
        return None
    s = pd.Series(pnl)
    gp, gl = s[s > 0].sum(), abs(s[s <= 0].sum())
    eq = pd.concat([pd.Series([CAPITAL]), pd.Series(caps)], ignore_index=True)
    return {"label": label, "trades": len(s), "open": 0, "win_rate": (s > 0).mean() * 100,
            "pf": gp / gl if gl > 0 else float("inf"),
            "max_dd": ((eq - eq.cummax()) / eq.cummax() * 100).min(),
            "return": (capital - CAPITAL) / CAPITAL * 100, "final_cap": capital,
            "giorni_medi": sum(giorni) / len(giorni)}


def _giornaliero(df):
    d = df[["Open", "High", "Low", "Close"]].dropna().copy()
    if getattr(d.index, "tz", None) is not None:
        d.index = d.index.tz_localize(None)
    return d


def trades_donchian(df, entra=55, esci=20, stop_atr=2.0, short=True):
    d = _giornaliero(df)
    d["hi"] = d["Close"].shift(1).rolling(entra).max()
    d["lo"] = d["Close"].shift(1).rolling(entra).min()
    d["ex_lo"] = d["Close"].shift(1).rolling(esci).min()
    d["ex_hi"] = d["Close"].shift(1).rolling(esci).max()
    d["atr"] = atr_calc(d, 20)
    d = d.dropna()
    O, H, L, C = d["Open"].values, d["High"].values, d["Low"].values, d["Close"].values
    HI, LO, XL, XH, A = d["hi"].values, d["lo"].values, d["ex_lo"].values, d["ex_hi"].values, d["atr"].values
    D, n, trades, i = d.index, len(d), [], 0
    while i < n - 1:
        segno = 1 if C[i] > HI[i] else (-1 if (short and C[i] < LO[i]) else 0)
        if segno == 0:
            i += 1
            continue
        e = i + 1
        entry, rischio = O[e], stop_atr * A[i]
        stop = entry - segno * rischio
        j, ex = e, None
        while j < n:
            if segno == 1 and (O[j] <= stop or L[j] <= stop):
                ex = min(O[j], stop); break
            if segno == -1 and (O[j] >= stop or H[j] >= stop):
                ex = max(O[j], stop); break
            if (segno == 1 and C[j] < XL[j]) or (segno == -1 and C[j] > XH[j]):
                if j + 1 < n:
                    j, ex = j + 1, O[j + 1]
                else:
                    ex = C[j]
                break
            j += 1
        if ex is None:
            j, ex = n - 1, C[n - 1]
        trades.append({"entry_date": D[e], "exit_date": D[j], "entry": entry, "exit": ex,
                       "dir": segno, "rischio": rischio})
        i = j
    return trades


def trades_medie(df, veloce=50, lenta=200, short=False):
    d = _giornaliero(df)
    d["f"], d["s"] = d["Close"].rolling(veloce).mean(), d["Close"].rolling(lenta).mean()
    d["atr"] = atr_calc(d, 20)
    d = d.dropna()
    O, C, F, S, A, D = d["Open"].values, d["Close"].values, d["f"].values, d["s"].values, d["atr"].values, d.index
    n, trades, pos, e = len(d), [], 0, None
    for i in range(n - 1):
        voluta = 1 if F[i] > S[i] else (-1 if short else 0)
        if voluta == pos:
            continue
        if pos != 0:                                    # chiude all'apertura dopo
            trades.append({"entry_date": D[e], "exit_date": D[i + 1], "entry": O[e], "exit": O[i + 1],
                           "dir": pos, "rischio": 2.0 * A[e - 1]})
        pos, e = voluta, (i + 1 if voluta != 0 else None)
    if pos != 0:
        trades.append({"entry_date": D[e], "exit_date": D[n - 1], "entry": O[e], "exit": C[n - 1],
                       "dir": pos, "rischio": 2.0 * A[e - 1]})
    return trades


def trades_fine_mese(df, giorni_dopo=3, stop_atr=3.0):
    d = _giornaliero(df)
    d["atr"] = atr_calc(d, 14)
    d = d.dropna()
    mesi = d.index.to_period("M")
    O, L, C, A, D = d["Open"].values, d["Low"].values, d["Close"].values, d["atr"].values, d.index
    trades = []
    for k in range(1, len(d) - giorni_dopo):
        if mesi[k] == mesi[k + 1]:                      # k = ultimo giorno di borsa del mese
            continue
        uscita = k + giorni_dopo                        # 3° giorno di borsa del mese dopo
        entry, rischio = O[k], stop_atr * A[k - 1]
        stop, ex, j = entry - rischio, None, uscita
        for t in range(k, uscita + 1):
            if O[t] <= stop or L[t] <= stop:
                ex, j = min(O[t], stop), t; break
        if ex is None:
            ex = C[uscita]
        trades.append({"entry_date": D[k], "exit_date": D[j], "entry": entry, "exit": ex,
                       "dir": 1, "rischio": rischio})
    return trades


def _sviluppo_verifica(trades, d0, d1, spread, finanz, prefisso):
    taglio = d0 + (d1 - d0) * 0.7
    righe = [
        _metriche_dir([t for t in trades if t["entry_date"] < taglio], f"{prefisso} sviluppo", spread, finanz),
        _metriche_dir([t for t in trades if t["entry_date"] >= taglio], f"{prefisso} VERIFICA", spread, finanz),
        _metriche_dir(trades, f"{prefisso} costi x2", spread * 2, finanz * 2),
    ]
    return [r for r in righe if r], taglio


def _stooq(simbolo):
    """Serie giornaliera dal CSV di Stooq (STOOQ_CSV), o None."""
    p = os.environ.get("STOOQ_CSV")
    if not p:
        return None
    raw = pd.read_csv(p, header=[0, 1], skiprows=[2], index_col=0, parse_dates=True)
    return pd.DataFrame({c: raw[(c, simbolo)] for c in ["Open", "High", "Low", "Close"]}).dropna()


def backtest_nuove():
    print("⏳ Dati giornalieri (oro, S&P500, altri mercati per robustezza)...")
    mercati = {"Oro (GC=F)": ("GC=F", COSTS["GOLD"], FINANZIAMENTO_ORO),
               "Argento (SI=F)": ("SI=F", COSTS["GOLD"], FINANZIAMENTO_ORO),
               "Petrolio (CL=F)": ("CL=F", COSTS["WTI"], 0.06),
               "Nasdaq 100": ("^NDX", COSTS["NASDAQ"], FINANZIAMENTO_ANNUO)}
    for nome, (tk, sp, fz) in mercati.items():
        df = get_data(tk, "max", "1d")
        if df is None:
            continue
        d = _giornaliero(df)
        bh = (d["Close"].iloc[-1] / d["Close"].iloc[0] - 1) * 100
        print(f"\n━━━━━━━━━━━━ TREND FOLLOWING — {nome} {d.index[0]:%Y}-{d.index[-1]:%Y} "
              f"(buy & hold {bh:+.0f}%) ━━━━━━━━━━━━")
        ris = []
        for etichetta, tr in (("Donchian L/S", trades_donchian(df)),
                              ("Donchian solo L", trades_donchian(df, short=False)),
                              ("Medie 50/200 L", trades_medie(df)),
                              ("Medie 50/200 L/S", trades_medie(df, short=True))):
            r, taglio = _sviluppo_verifica(tr, d.index[0], d.index[-1], sp, fz, etichetta)
            ris += r
        print_results(ris)
        print(f"Taglio sviluppo/verifica: {taglio:%Y-%m-%d}")
    st = _stooq("GC.F")
    if st is not None:
        print(f"\n━━━━━━━━━━━━ TREND FOLLOWING — Oro Stooq {st.index[0]:%Y}-{st.index[-1]:%Y} ━━━━━━━━━━━━")
        ris = []
        for etichetta, tr in (("Donchian L/S", trades_donchian(st)), ("Medie 50/200 L", trades_medie(st))):
            ris += _sviluppo_verifica(tr, st.index[0], st.index[-1], COSTS["GOLD"], FINANZIAMENTO_ORO, etichetta)[0]
        print_results(ris)

    df = get_data("^GSPC", "max", "1d")
    if df is not None:
        d = _giornaliero(df)
        d = d[d.index >= "1950-01-01"]
        print(f"\n━━━━━━━━━━━━ INIZIO/FINE MESE — S&P500 {d.index[0]:%Y}-{d.index[-1]:%Y} ━━━━━━━━━━━━")
        tr = trades_fine_mese(d)
        ris, taglio = _sviluppo_verifica(tr, d.index[0], d.index[-1], COSTS["SP500"], FINANZIAMENTO_ANNUO,
                                         "Fine mese")
        recenti = [t for t in tr if t["entry_date"] >= pd.Timestamp("2006-01-01")]
        ris.append(_metriche_dir(recenti, "Fine mese dal 2006", COSTS["SP500"], FINANZIAMENTO_ANNUO))
        print_results([r for r in ris if r])
        print(f"Taglio sviluppo/verifica: {taglio:%Y-%m-%d}")
        # Confronto grezzo: rendimento medio giornaliero dentro e fuori dalla finestra
        r = d["Close"].pct_change()
        mesi = d.index.to_period("M")
        dentro = pd.Series(False, index=d.index)
        for k in range(len(d) - 1):
            if mesi[k] != mesi[k + 1]:
                dentro.iloc[k:k + 4] = True             # ultimo giorno + primi 3
        for periodo, maschera in (("1950-oggi", d.index >= "1950-01-01"), ("dal 2006", d.index >= "2006-01-01")):
            a, b = r[dentro & maschera].mean() * 100, r[~dentro & maschera].mean() * 100
            print(f" Rendimento medio giornaliero {periodo}: finestra {a:+.3f}% | altri giorni {b:+.3f}%")

    print("\nCome leggere: conta la VERIFICA (anni non usati per scegliere nulla) e la riga costi x2.")
    print("Trend following: pochi trade vincenti e grandi, molti perdenti e piccoli; anni negativi normali.")
    print("A2 'Medie' non ha stop: il rischio reale per trade è più alto dell'1% indicato.\n")


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
    elif len(sys.argv) > 1 and sys.argv[1] == "nuove":
        backtest_nuove()
    elif len(sys.argv) > 1 and sys.argv[1] == "bot":
        print("🔍 Backtest delle strategie 1, 2, 4, 5 del bot...\n")
        results = [
            backtest_vwap_reversal_oro(),
            backtest_london_sweep_oro(),
            backtest_kumo_oro_4h(),
            backtest_kumo_nasdaq_1h(),
        ]
        print_results([r for r in results if r is not None])
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
