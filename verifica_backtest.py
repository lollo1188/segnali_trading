"""
VERIFICA BOT ↔ BACKTEST — python3 verifica_backtest.py

Per le strategie 1, 2, 3, 4, 5 e 9 (VWAP Reversal, London Sweep, ORB S&P500, Kumo Oro 4H,
Kumo Nasdaq 1H, Ritracciamento S&P500)
riesegue le funzioni VERE del bot (strategies.py) su ogni controllo storico:
 - agli stessi orari dello scheduler di main.py;
 - con i dati che Yahoo avrebbe restituito in quel momento (candela in formazione inclusa);
 - con la stessa pausa tra segnali di main.should_send.
Poi confronta i segnali con quelli di `backtest.py bot`, `orb` e `trend`. Se coincidono, il backtest
misura esattamente ciò che fa il bot. Richiede rete verso Yahoo Finance (~2 minuti).
Filtro notizie non applicato (né qui né nel backtest).
"""
import os, sys, time as T
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warnings; warnings.filterwarnings("ignore")
from datetime import datetime, time, timedelta
import pandas as pd, yfinance as yf
import strategies as st, backtest as bt

ROME, ET = st.ROME_TZ, st.ET_TZ


def scarica(ticker, period, interval):
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ROME)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


DATI = {}
def dati(ticker, period, interval):
    k = (ticker, period, interval)
    if k not in DATI:
        DATI[k] = scarica(*k)
    return DATI[k]


class Orologio:
    now_val = None
class FintoDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return Orologio.now_val.astimezone(tz) if tz else Orologio.now_val
st.datetime = FintoDatetime
st.adesso_et = lambda: Orologio.now_val.astimezone(ET)


def replay(nome, fn, df, orari, cooldown_h, finestra_job=None, max_righe=None):
    """Esegue fn() a ogni orario dello scheduler con i dati disponibili in quel momento.
    Pausa come main.should_send: blocca solo se l'ULTIMO segnale ha la stessa direzione."""
    segnali, ultimo = [], None
    idx = df.index
    for now in orari:
        if finestra_job and not finestra_job(now):
            continue
        n = idx.searchsorted(now, side="right")       # candele iniziate entro "now"
        if n < 3:
            continue
        sl = df.iloc[max(0, n - max_righe):n] if max_righe else df.iloc[:n]
        Orologio.now_val = now
        st.get_data = lambda *a, **k: sl.copy()
        sig = fn()
        if not sig:
            continue
        if ultimo and ultimo[0] == sig["direzione"] and (now - ultimo[1]) < timedelta(hours=cooldown_h):
            continue
        ultimo = (sig["direzione"], now)
        segnali.append((sl.index[-2], sig["direzione"], float(sig["entry"]), now))
    return segnali


def orari_ogni(df, minuti, offset):
    t0 = df.index[0].floor("D")
    return pd.date_range(t0, df.index[-1] + pd.Timedelta(minutes=minuti),
                         freq=f"{minuti}min").tz_convert(ROME) + pd.Timedelta(minutes=offset)


def backtest_segnali(fn, ticker, period, interval):
    raw = dati(ticker, period, interval)
    bt.get_data = lambda *a, **k: raw.copy()
    presi = {}
    orig = bt.simulate
    def cattura(sdf, cost, label):
        presi["df"] = sdf
        return orig(sdf, cost, label)
    bt.simulate = cattura
    fn()
    bt.simulate = orig
    sdf = presi.get("df")
    if sdf is None:
        return []
    return [(r.ts, "LONG" if r.direction == 1 else "SHORT", round(float(r.entry), 2))
            for r in sdf.itertuples()]


def confronta(nome, live, back, dal=None):
    """dal: ignora i segnali precedenti (inizio dati: il bot vuole almeno 40 candele di storia,
    che dal vivo ha sempre, mentre all'inizio dello storico scaricato non ci sono)."""
    L = {(ts, d): e for ts, d, e, _ in live if dal is None or ts >= dal}
    B = {(ts, d): e for ts, d, e in back if dal is None or ts >= dal}
    comuni = set(L) & set(B)
    diff_prezzo = [k for k in comuni if abs(L[k] - B[k]) > 0.01 * max(1, abs(B[k])) / 100]
    print(f"\n== {nome}: bot {len(L)} segnali | backtest {len(B)} | in comune {len(comuni)}"
          f" | prezzi diversi {len(diff_prezzo)}")
    for k in sorted(set(L) - set(B)):
        print(f"   solo BOT      {k[0]:%d/%m/%Y %H:%M} {k[1]:5} entry {L[k]:.2f}")
    for k in sorted(set(B) - set(L)):
        print(f"   solo BACKTEST {k[0]:%d/%m/%Y %H:%M} {k[1]:5} entry {B[k]:.2f}")
    for k in sorted(diff_prezzo):
        print(f"   prezzo diverso {k[0]:%d/%m/%Y %H:%M} bot {L[k]:.2f} backtest {B[k]:.2f}")


t0 = T.time()
# 1) VWAP Reversal Oro: ai :02, :17, :32, :47 (come main.py), dati "5d"
g15 = dati("GC=F", "60d", "15m")
live = replay("vwap", st.check_vwap_reversal_gold, g15, orari_ogni(g15, 15, 2), 4, max_righe=480)
confronta("VWAP REVERSAL ORO", live, backtest_segnali(bt.backtest_vwap_reversal_oro, "GC=F", "60d", "15m"))
print(f"   ({T.time()-t0:.0f}s)")

# 2) London Sweep: ogni 15 minuti tra 08:00 e 10:00 italiane
live = replay("london", st.check_london_sweep_gold, g15, orari_ogni(g15, 15, 2), 12,
              finestra_job=lambda n: time(8, 0) <= n.time() <= time(10, 0), max_righe=300)
confronta("LONDON SWEEP ORO", live, backtest_segnali(bt.backtest_london_sweep_oro, "GC=F", "60d", "15m"))
print(f"   ({T.time()-t0:.0f}s)")

# 3) Kumo Oro 4H: ogni ora ai :02 (come main.py), dati orari "2y"
g1h = dati("GC=F", "2y", "1h")
live = replay("kumo_oro", st.check_kumo_gold_4h, g1h, orari_ogni(g1h, 60, 2), 12)
back = backtest_segnali(bt.backtest_kumo_oro_4h, "GC=F", "2y", "1h")
# Confronto per direzione e prezzo (l'orario registrato dal replay è l'ultima candela ORARIA)
Lk = {(d, round(e, 2)): now for _, d, e, now in live}
Bk = {(d, e): ts for ts, d, e in back}
comuni = set(Lk) & set(Bk)
print(f"\n== KUMO RIDER ORO 4H: bot {len(Lk)} | backtest {len(Bk)} | in comune {len(comuni)}")
for k in sorted(set(Lk) ^ set(Bk)):
    print("   diverso:", k, "solo bot" if k in Lk else "solo backtest")
for k in sorted(comuni, key=lambda k: Bk[k]):
    ritardo = (Lk[k] - (Bk[k] + pd.Timedelta(hours=4))).total_seconds() / 60
    print(f"   candela {Bk[k]:%d/%m/%Y %H:%M} {k[0]:5} {k[1]:.2f} → inviato {ritardo:.0f} min dopo la chiusura")
print(f"   ({T.time()-t0:.0f}s)")

# 4) Kumo Nasdaq 1H: ai :32 di ogni ora, finestra 09:30–15:35 di New York (come main.py)
n1h = dati("^NDX", "2y", "1h")
live = replay("kumo_ndx", st.check_kumo_nasdaq_1h, n1h, orari_ogni(n1h, 60, 32), 8,
              finestra_job=lambda n: time(9, 30) <= n.astimezone(ET).time() <= time(15, 35))
confronta("KUMO RIDER NASDAQ 1H", live, backtest_segnali(bt.backtest_kumo_nasdaq_1h, "^NDX", "2y", "1h"))
print(f"   ({T.time()-t0:.0f}s)")

# 5) ORB S&P500: ai :02, :17, :32, :47 tra 09:45 e 15:45 di New York, dati "5d"
s15 = dati("^GSPC", "60d", "15m")
live = replay("orb_sp500", st.check_orb_sp500, s15, orari_ogni(s15, 15, 2), 8,
              finestra_job=lambda n: time(9, 45) <= n.astimezone(ET).time() <= time(15, 45),
              max_righe=140)
confronta("ORB S&P500", live, backtest_segnali(bt.backtest_orb_sp500, "^GSPC", "60d", "15m"),
          dal=s15.index[60])          # dal 3° giorno di dati
print(f"   ({T.time()-t0:.0f}s)")

# 6) Ritracciamento S&P500: ogni sera alle 16:10 di New York con dati giornalieri "2y".
#    Confronto ingressi ed eventi di uscita con backtest._trades_pullback (uscita all'apertura dopo).
import news_filter as nf
g1d = scarica("^GSPC", "6y", "1d")
giorni_borsa = [ts.date() for ts in g1d.index]
# Giorno d'ingresso = giorno di borsa successivo nei dati (isola la logica dal calendario festività)
nf.prossimo_giorno_usa = lambda d: next((x for x in giorni_borsa if x > d), d + timedelta(days=1))
inizio = len(g1d) - 1000                                   # ~4 anni di sere
pos, eventi_bot = None, []
for k in range(inizio, len(g1d)):
    giorno = g1d.index[k].date()
    Orologio.now_val = ET.localize(datetime.combine(giorno, time(16, 10))).astimezone(ROME)
    fetta = g1d.iloc[max(0, k + 1 - 504):k + 1]            # "2y" ≈ 504 sedute
    st.get_data = lambda *a, **k: fetta.copy()
    msg, pos, _ = st.analizza_pullback("^GSPC", "pullback_sp500", "S&P 500", pos)
    if msg and msg["tipo"] in ("ENTRATA", "USCITA", "TEMPO", "STOP"):
        eventi_bot.append((msg["tipo"], msg["data"]))

raw = g1d.copy(); raw.index = raw.index.tz_localize(None)
trades = bt._trades_pullback(raw, uscita_apertura_dopo=True)[0]
primo = g1d.index[inizio].date()
eventi_bt = []
for t in trades:
    sd = t["signal_date"].date()
    if sd < primo:
        continue
    eventi_bt.append(("ENTRATA", sd))
    if t["motivo"] == "fine dati":
        continue
    tipo = {"uscita": "USCITA", "tempo": "TEMPO", "stop": "STOP"}[t["motivo"]]
    # con l'uscita all'apertura dopo, l'evento (segnale di uscita) è la sera prima
    giorno_ev = t["exit_date"].date()
    if tipo in ("USCITA", "TEMPO"):
        giorno_ev = max(d for d in giorni_borsa if d < giorno_ev)
    eventi_bt.append((tipo, giorno_ev))
LB, BB = set(eventi_bot), set(eventi_bt)
print(f"\n== RITRACCIAMENTO S&P500 (dal {primo:%d/%m/%Y}): eventi bot {len(LB)} | backtest {len(BB)}"
      f" | in comune {len(LB & BB)}")
for e in sorted(LB - BB, key=lambda x: x[1]):
    print(f"   solo BOT      {e[1]:%d/%m/%Y} {e[0]}")
for e in sorted(BB - LB, key=lambda x: x[1]):
    print(f"   solo BACKTEST {e[1]:%d/%m/%Y} {e[0]}")
print(f"   ({T.time()-t0:.0f}s)")
