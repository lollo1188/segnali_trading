# 🤖 Trading Bot — Revolut CFD Signal System

Bot di segnali semi-automatico per i CFD di Revolut. Gira H24 su Railway e ti manda gli alert su Telegram: **tu controlli il segnale ed esegui a mano su Revolut**. Il bot non apre, non segue e non chiude nessuna operazione.

---

## ⚙️ Strategie incluse

| # | Nome | Asset | Timeframe | Logica | Finestra CET |
|---|------|-------|-----------|--------|--------------|
| 1 | VWAP Reversal | Oro | 15min | Prezzo oltre ±2σ dal VWAP + RSI estremo + candela di inversione | 08:00–21:45 |
| 2 | London Sweep | Oro | 15min | Falso breakdown del range asiatico (06:00–08:00) | 08:00–10:00 |
| 3 | ORB S&P500 | S&P 500 | 15min | Breakout della prima candela USA (15:30) + VWAP + EMA20 + RSI | 15:45–21:45 |
| 4 | Kumo Rider Oro | Oro | 4H (swing) | Ichimoku 9/26/52 + RSI | sempre |
| 5 | Kumo Rider Nasdaq | Nasdaq 100 | 1H (swing) | Ichimoku 9/26/52 + RSI | 15:30–21:00 |

Le strategie 1, 2 e 3 sono **intraday**: chiudi entro le 21:45 CET. Le strategie 4 e 5 possono restare aperte overnight.

Dopo un segnale, la stessa strategia nella stessa direzione non viene riproposta per un periodo di pausa (4–12 ore a seconda della strategia). Questa pausa è tenuta in memoria: si azzera a ogni riavvio del bot.

---

## 💬 Formato dei segnali Telegram

Esempio di segnale ORB:

```
🟢 ORB S&P500
━━━━━━━━━━━━━━━━━━━━
📊 Asset: S&P 500
⏱ TF: 15min
🕯 Candela: 16:15–16:30 CET
🎯 Entry: 7277.96
🛑 Stop: 7152.66
✅ Target: 7591.21
📐 R:R: 1:2.5
━━━━━━━━━━━━━━━━━━━━
⏱ SLITTAMENTO: +17.04 pt (+0.23%) — 🟡 Slittamento moderato
Prezzo ora 7295.00 vs entry 7277.96 (14% del rischio contro di te)
📐 R:R entrando ora: 1:2.1 (stop e target del segnale)
🔁 Stesse distanze dal prezzo ora → Stop 7169.70 | TP1 7482.95 | Target 7608.25
ℹ️ Prezzo Yahoo: confrontalo con quello su Revolut
━━━━━━━━━━━━━━━━━━━━
📝 Breakout OR high 7260.17 | Sopra VWAP | TP1 7465.91 → sposta SL a BE | Chiudi entro 21:45
⏱ INTRADAY — chiudi entro 21:45 CET
⚠️ Rischia max 1.5% capitale
🕐 Inviato 23/11 16:32
```

### Come si legge

- **Candela**: la candela su cui il bot ha deciso. L'entry è la chiusura di quella candela.
- **Entry / Stop / Target**: livelli teorici calcolati sui dati di Yahoo Finance.
- **R:R**: rapporto rischio/rendimento del segnale. È un testo calcolato con stop e target originali.
- **Inviato**: ora di invio, non della candela. Più è lontana dalla candela, più il segnale è vecchio.
- **TP1 → sposta SL a BE** (solo ORB): arrivato a TP1, porta lo stop sul prezzo d'ingresso.

### Lo slittamento

Il bot confronta l'entry con l'ultimo prezzo disponibile al momento dell'invio:

| Segnale | Significato |
|---------|-------------|
| 🟢 Trascurabile | Fino al 10% del rischio |
| 🟡 Moderato | Tra il 10% e il 25% |
| 🔴 Forte | Oltre il 25%: meglio aspettare |
| ⛔ Non entrare | Stop o target già superati, oppure R:R sceso sotto 1 |

"Contro di te" vuol dire che entreresti a un prezzo peggiore di quello del segnale; "a tuo favore" il contrario. Il blocco mostra anche il **R:R entrando ora** (con stop e target originali) e i livelli **ricalcolati con le stesse distanze** dal prezzo attuale. Se il prezzo attuale non è disponibile, il blocco viene omesso.

> ℹ️ Il prezzo usato è quello di **Yahoo Finance**, non quello di Revolut. I CFD di Revolut hanno un prezzo e uno spread propri: confronta sempre con l'app prima di entrare.

---

## 📊 Come eseguire su Revolut

1. Ricevi la notifica Telegram e controlla la riga **SLITTAMENTO**
2. Apri Revolut → Trading → cerca l'asset
3. Imposta direzione, Stop Loss e Take Profit (quelli del segnale, oppure quelli ricalcolati se entri a un prezzo diverso)
4. Calcola la size e conferma

### Calcolo della size

```
Rischio € = Capitale × % di rischio
Size = Rischio € / |Entry − Stop Loss|
```

**Esempio:** capitale €1.000, rischio 1%, entry 7277.96, stop 7152.66 → rischio €10, distanza 125.30 punti → size ≈ 0.08.

La percentuale di rischio la decidi tu. La scritta "Rischia max 1.5%" nel messaggio è un testo fisso: il bot non conosce il tuo capitale.

---

## ⚠️ Regole fondamentali

- Decidi in anticipo la percentuale di rischio per trade e non superarla
- Dopo 3 stop loss consecutivi: stop per la giornata
- Controlla [forexfactory.com](https://forexfactory.com) la mattina (notizie rosse)
- S&P 500 e Nasdaq sono correlati: non aprire entrambi nella stessa direzione
- Usa prima un conto demo / paper trading

---

## 🔬 Backtest

```bash
python3 backtest.py        # 4 strategie storiche (Surfista, Pendolo, Rompighiaccio, Barile Caldo)
python3 backtest.py orb    # confronto varianti ORB S&P500 (60 giorni)
```

Il calcolo del **Profit Factor** usa tutti i trade, anche quelli chiusi a fine sessione senza toccare stop né target ("Open"), al netto dei costi. La colonna `Open` ti dice quanti sono.

**Limiti da tenere presenti:**
- Per i dati a 15 minuti Yahoo Finance fornisce solo circa 60 giorni: l'ORB viene testato su poche decine di trade, troppo poche per un PF affidabile.
- Il backtest non include le strategie VWAP Reversal, London Sweep e Kumo Rider: **i PF indicati nei commenti del codice non sono verificati da nessuno script del repository**.
- Il backtest simula un solo trade al giorno e non gestisce il TP1 con break-even.
- Il filtro sull'ampiezza dell'opening range usa l'ATR dell'intera giornata, quindi anche candele future (lookahead).

---

## 🧱 Limiti noti del bot

- **Dati Yahoo Finance**, non il prezzo del CFD di Revolut.
- I controlli girano ogni 15 minuti dall'avvio, non allineati alle candele: l'entry può essere vecchia fino a circa 15 minuti. Per questo esiste il blocco slittamento.
- L'opening range è fissato alle 15:30 CET. Nelle settimane in cui l'ora legale USA ed Europa è sfasata (marzo e fine ottobre/inizio novembre) l'apertura USA cade alle 14:30 e il bot userebbe la candela sbagliata.
- Il bot non conosce i giorni festivi o le mezze sedute di Wall Street: in quei giorni può proporre segnali basati su candele vecchie. Ignorali.
- Non segue le operazioni: non controlla stop e target e non ti avvisa del TP1 o della chiusura serale.
- Il limite "max 4% totale" nel messaggio di avvio è solo un promemoria.

---

## 🚀 Deploy su Railway — Istruzioni passo passo

### STEP 1 — Carica il codice su GitHub

1. Vai su [github.com](https://github.com) → **New repository**
2. Dagli un nome (es. `trading-bot`) → **Create repository**
3. Carica tutti i file: `main.py`, `strategies.py`, `notifier.py`, `news_filter.py`, `backtest.py`, `requirements.txt`, `Procfile`, `railway.toml`
4. Clicca **Commit changes**

### STEP 2 — Crea account Railway

1. Vai su [railway.app](https://railway.app)
2. **Start a New Project** e accedi con il tuo account **GitHub**

### STEP 3 — Crea il progetto

1. **Deploy from GitHub repo** e seleziona il repository
2. Railway avvia il deploy in automatico

### STEP 4 — Variabili d'ambiente

Nel tab **Variables** aggiungi:

- `TELEGRAM_TOKEN` = il token che ti ha dato BotFather
- `CHAT_ID` = il tuo ID numerico Telegram

Railway riavvia il bot da solo.

### STEP 5 — Verifica

1. Tab **Deployments**: lo stato deve essere verde
2. Dopo circa 30 secondi ricevi su Telegram "🤖 Trading Bot — Avviato"
3. Se non arriva, controlla i **Logs**

---

## 🔧 Manutenzione

- Il bot gira in automatico H24 su Railway
- Verifica il tuo piano Railway: un bot attivo tutto il mese usa circa 720 ore, quindi un limite di 500 ore/mese non basterebbe
- Se il bot si ferma: Railway → **Restart**
- Per aggiornare il codice: modifica i file su GitHub, Railway si aggiorna da solo (la pausa tra segnali si azzera a ogni deploy, quindi può arrivare un segnale ripetuto)
- Nei log non compare il token Telegram, ma controlla che non finisca mai in un messaggio d'errore copiato in giro

---

*Non costituisce consulenza finanziaria. Il trading con leva comporta rischio di perdita del capitale.*


## 📊 Come eseguire su Revolut

1. Ricevi notifica Telegram
2. Apri Revolut → Trading → cerca l'asset
3. Imposta:
   - **Direzione:** Long o Short
   - **Stop Loss:** il valore indicato
   - **Take Profit:** il valore indicato
   - **Size:** in base alla regola 1% (vedi sotto)
4. Conferma il trade

### Calcolo della size (regola 1%)

```
Rischio € = Capitale × 1%
Size = Rischio € / (Entry - Stop Loss)
```

**Esempio:** Capitale €1.000, Entry 5120, SL 5095 → Rischio €10 → Size = 10/25 = 0.4 contratti

---

## ⚠️ Regole fondamentali

- Mai rischiare più dell'1% del capitale per trade
- Dopo 3 stop loss consecutivi: stop per la giornata
- Controlla sempre [forexfactory.com](https://forexfactory.com) la mattina (notizie rosse)
- WTI: verifica la scadenza del future ogni mese (terzo venerdì)
- S&P500 e Nasdaq sono correlati — non aprire entrambi nella stessa direzione

---

## 🔧 Manutenzione

- Il bot gira in automatico H24 su Railway
- Railway free tier: 500 ore/mese (sufficiente per un bot worker)
- Se il bot si ferma: vai su Railway → "Restart"
- Per aggiornare il codice: modifica i file su GitHub → Railway si aggiorna automaticamente

---

*Non costituisce consulenza finanziaria. Il trading con leva comporta rischio di perdita del capitale.*
