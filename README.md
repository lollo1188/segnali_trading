# 🤖 Trading Bot — Revolut CFD Signal System

Bot di segnali semi-automatico per i CFD di Revolut. Gira H24 su Railway e ti manda gli alert su Telegram: **tu controlli il segnale ed esegui a mano su Revolut**. Il bot non apre, non segue e non chiude nessuna operazione.

---

## ⚙️ Strategie incluse

| # | Nome | Asset | Timeframe | Logica | Finestra (ora italiana) |
|---|------|-------|-----------|--------|--------------|
| 1 | VWAP Reversal | Oro | 15min | Prezzo oltre ±2σ dal VWAP + RSI estremo + candela di inversione | 08:00–21:45 |
| 2 | London Sweep | Oro | 15min | Falso breakdown del range asiatico (06:00–08:00) | 08:00–10:00 |
| 3 | ORB S&P500 | S&P 500 | 15min | Breakout della prima candela USA (09:30 New York, di solito 15:30 italiane) + VWAP + EMA20 + RSI | 15:45–21:45 |
| 4 | Kumo Rider Oro | Oro | 4H (swing) | Ichimoku 9/26/52 + RSI | sempre |
| 5 | Kumo Rider Nasdaq | Nasdaq 100 | 1H (swing) | Ichimoku 9/26/52 + RSI | 15:30–21:00 |
| 6 | ORB Anthropic | ANT (Nasdaq) | 15min | ORB adattata a un titolo singolo | 16:00–20:30 |
| 7 | ORB Caterpillar | CAT (NYSE) | 15min | ORB adattata a un titolo singolo | 16:00–20:30 |
| 8 | ORB Broadcom | AVGO (Nasdaq) | 15min | ORB adattata a un titolo singolo | 16:00–20:30 |
| 9 | Ritracciamento S&P500 | S&P 500 | Giornaliero | RSI(2) < 10 sopra media 200gg, solo long | Controllo ~22:10 |
| 10 | Ritracciamento Broadcom | AVGO | Giornaliero | Come sopra | Controllo ~22:10 |

Le strategie 1, 2, 3, 6, 7 e 8 sono **intraday**: chiudi entro l'ora indicata nel messaggio (21:45 italiane; per le strategie USA 20:45 nelle settimane con ora legale sfasata). Le strategie 4 e 5 possono restare aperte overnight. Gli orari delle strategie USA (3, 5, 6–10) seguono l'ora di New York: vedi *Quando arrivano i segnali*.

Dopo un segnale, la stessa strategia nella stessa direzione non viene riproposta per un periodo di pausa (4–12 ore a seconda della strategia). Questa pausa è salvata nel file di stato e sopravvive ai riavvii, se su Railway c'è il volume `/data` (vedi STEP 4b).

### Strategie su azioni singole (6, 7, 8)

Stessa logica dell'ORB S&P500 (breakout della candela delle 15:30 con VWAP, EMA20 e RSI), con adattamenti ai titoli singoli:

| | ANT | CAT | AVGO |
|---|---|---|---|
| Stop massimo (% del prezzo) | 5% | 3% | 4% |
| Short per default | disattivato | attivo | attivo |
| Backtest | **impossibile** (quotata dal 19/11/2026) | `backtest.py azioni` | `backtest.py azioni` |

- Il segnale parte solo se la candela è di oggi e chiusa da meno di 25 minuti (evita dati vecchi in giorni festivi o mezze sedute).
- Gli stop massimi in percentuale sono valori **arbitrari**, non ottimizzati.
- Tutti i parametri sono **non validati**: usa solo demo o rischio minimo.
- **Giorni dei risultati trimestrali**: il bot li salta in automatico solo se la data è in `UTILI` di `news_filter.py` (oggi AVGO 10/12/2026 e CAT 28/1/2027; ANT non ancora). Quei giorni hanno gap e volatilità anomali: aggiorna il calendario a ogni trimestre.
- ANT e AVGO sono entrambi legati al tema AI e AVGO si muove spesso con il Nasdaq: più segnali nella stessa direzione non sono indipendenti.
- Non è verificato che Revolut offra i CFD su ANT, CAT e AVGO: controlla nell'app.

### Ritracciamento nel trend giornaliero (9 e 10)

Segnale a fine seduta USA, **ingresso all'apertura del giorno dopo**, solo long:
- **Trend**: chiusura sopra la media a 200 giorni. **Ingresso**: RSI(2) sotto 10.
- **Uscita**: la chiusura supera la media a 5 giorni (si vende all'apertura dopo), oppure stop a 3×ATR dall'ingresso, oppure massimo 10 giorni.
- Il bot controlla alle 16:10 e alle 16:40 di New York (di solito 22:10 e 22:40 italiane, lun–ven) e manda: **ENTRATA** (con stop e livello d'uscita), poi un promemoria serale **MANTIENI / ESCI / STOP**.
- **Il bot non conosce le tue posizioni**: segue una posizione *teorica* dal segnale. Se non apri il CFD, ignora i promemoria. La posizione teorica è salvata su file e sopravvive ai riavvii, **purché su Railway ci sia il volume `/data`** (STEP 4b). Senza volume si perde a ogni deploy: in quel caso gestisci l'uscita a mano con la regola sopra.
- Se il giorno d'ingresso previsto è una festività non presente nel calendario, l'ingresso teorico diventa il primo giorno di borsa successivo (prima la posizione teorica veniva scartata e i promemoria si fermavano).
- Se l'ingresso cade nel giorno dei risultati trimestrali (`UTILI` in `news_filter.py`) il segnale viene saltato e te lo dice. Se domani c'è un evento in calendario (NFP, CPI, FOMC) il messaggio lo segnala.
- Risultati del backtest (`trend`, `trend_azioni`): S&P 500 PF ≈1,43 sia nello sviluppo sia nella verifica (506 trade dal 1993); AVGO PF 1,39 e 1,77 (270 trade dal 2010); **CAT non mostra vantaggio e non è inclusa**. Il vantaggio per trade è piccolo (circa +1% l'anno a rischio 1%) e AVGO è un titolo con un trend eccezionale: **prova solo in demo per 4–8 settimane**.
- Il backtest esce alla chiusura, il live all'apertura del giorno dopo: la riga "Uscita apertura dopo (live)" di `python3 backtest.py trend` mostra quanto cambia.

---

## 🕐 Quando arrivano i segnali (ora italiana, lun–ven)

Le strategie USA (ORB, Kumo Nasdaq, ritracciamento) seguono l'**ora di New York**: gli orari sotto valgono per la maggior parte dell'anno. Nelle settimane in cui l'ora legale USA ed Europa è sfasata (marzo e fine ottobre/inizio novembre) arrivano **un'ora prima** (es. ORB S&P500 dalle 15:00, chiusura intraday alle 20:45). Il messaggio indica sempre l'ora giusta di chiusura. Nelle **mezze sedute** del calendario (es. 24/12 e il giorno dopo Thanksgiving, chiusura alle 19:00 italiane) gli ORB smettono di cercare ingressi alle 18:45 e il messaggio dice "chiudi entro le 18:45"; anche il registro chiude lì le posizioni teoriche.

| Strategia | Orari possibili | Note |
|-----------|-----------------|------|
| VWAP Reversal Oro | ~08:15–21:45 | Controllo ogni 15 min; ultima candela 21:15 |
| London Sweep Oro | ~08:30–10:00 | Solo long |
| ORB S&P500 | ~16:02–21:32 | Al massimo uno al giorno per direzione (può esserci un LONG e uno SHORT) |
| ORB ANT / CAT / AVGO | ~16:00–20:30 | Come sopra |
| Kumo Rider Oro 4H | ~2 minuti dopo la chiusura di una candela 4H (controllo ogni ora) | Raro: serve un incrocio Ichimoku |
| Kumo Rider Nasdaq 1H | ~16:32, 17:32, 18:32, 19:32, 20:32, 21:32 (2 minuti dopo ogni candela oraria) | Raro |
| Ritracciamento S&P500 / AVGO | ~22:10 e 22:40, lun–ven | Pochi segnali all'anno; promemoria serali finché la posizione teorica è aperta |

Sono finestre possibili, non garantite: il segnale arriva solo se le condizioni sono vere. Nel weekend e nei giorni festivi USA il bot è silenzioso. In più il filtro notizie (sotto) può saltare alcuni controlli.

---

## 🛡️ Filtro notizie (`news_filter.py`)

Il bot salta i controlli nelle finestre ad alto rischio. **Non usa un calendario online**: usa le date scritte in `news_filter.py`, che **devi aggiornare tu**.

Le finestre sono definite in **ora di New York** e convertite in ora italiana per ogni data, quindi restano corrette anche quando ora legale USA ed Europa sono sfasate.

| Regola | Quando | Chi riguarda |
|--------|--------|--------------|
| Dati USA 08:30 ET (≈14:25–14:50 italiane) | Ogni giorno feriale | Strategie intraday (non le Kumo) |
| Eventi ad alto impatto (NFP, CPI, FOMC) | Solo nelle date in elenco | Tutte le strategie |
| Wall Street chiusa / mezza seduta | Date in elenco | ORB, Kumo Nasdaq |
| Oro chiuso | Natale, Capodanno | Strategie sull'oro |
| Giorno dei risultati trimestrali | Date in elenco per titolo | ORB sul singolo titolo |

**Calendario inserito** (da verificare periodicamente su BLS, Federal Reserve, NYSE e comunicati delle società):
- NFP e CPI: da dicembre 2026 a dicembre 2027 (tutte le uscite mensili)
- FOMC: 9/12/2026 e le 7 decisioni del 2027 (27/1, 17/3, 28/4, 9/6, 28/7, 15/9, 17/11), dal calendario Fed rivisto il 12/11/2026. La riunione del 26-27/10/2027 è solo di strategia, senza decisione
- Chiusure USA 2026-2027 complete (fonte NYSE); mezze sedute: 27/11/2026, 24/12/2026, 26/11/2027
- Utili Broadcom: reazione del titolo il 10/12/2026 (risultati il 9/12 dopo la chiusura)
- Utili Caterpillar: 28/1/2027 prima dell'apertura (reazione lo stesso giorno)
- **Da completare**: utili di Anthropic; calendario 2028

**Ripresa dei controlli:** se un controllo viene saltato per una notizia e il blocco finisce entro 2 ore, il bot lo ripete un minuto dopo la fine del blocco, così la candela non va persa. Per blocchi lunghi (festivi, giorno degli utili) non ripete.

**Limiti da conoscere:**
- Il calendario si aggiorna a mano: se manca una data, il bot non blocca nulla.
- Non copre ISM, discorsi Fed, PCE e altri dati non inseriti, né notizie improvvise.
- La data degli utili di ANT non è ancora inserita.
- Il calendario copre fino a dicembre 2027: va esteso al 2028.
- Gli orari dell'oro nei giorni festivi sono un'approssimazione: verifica quelli del tuo CFD.
- Le strategie USA usano anch'esse l'ora di New York (opening range = candela delle 09:30 ET). Le strategie sull'oro restano in ora italiana.

---

## 📒 Registro dei segnali e resoconto settimanale (`registro.py`)

Ogni segnale inviato viene salvato in `registro.json`, accanto al file di stato (su Railway il volume `/data`). Ogni 30 minuti il bot controlla sulle candele successive come sarebbe andato, con **le stesse regole del backtest**:
- stop o target toccati (se nella stessa candela succedono entrambi conta lo stop);
- altrimenti chiusura forzata: intraday all'ora di chiusura del messaggio (21:45 italiane per l'oro, 15:45 di New York per l'ORB), Kumo dopo 14 giorni.

Il risultato è in **R** (multipli del rischio: +2,5R = target dell'ORB, −1R = stop), al netto dello spread stimato.

**Ogni sabato alle 10:07** arriva su Telegram il resoconto: per ogni strategia segnali della settimana e totale da inizio registro, vinti/persi, R totale, PF, slittamento medio al momento dell'invio e ritardo medio rispetto alla chiusura della candela.

- È un esito **teorico**: non sa se hai aperto il CFD né a che prezzo. Non simula TP1, break-even e trailing (come il backtest).
- Il ritracciamento giornaliero (9, 10) non è incluso: ha già i suoi promemoria serali.
- **Modalità "solo registro"**: le strategie elencate nella variabile `STRATEGIE_OMBRA` (es. `orb_cat,orb_avgo`) girano normalmente ma **non mandano segnali su Telegram**. Finiscono solo nel registro e nel resoconto, segnate 👻 *solo registro*, e non contano nel rischio totale. Serve a misurare una strategia senza riceverla. Vince su `STRATEGIE_SPENTE`, quindi può restare anche lì. Non vale per il ritracciamento giornaliero.
- Con meno di circa 30 segnali per strategia non trarre conclusioni.

---

## 💼 Rischio totale nel messaggio

Se ci sono altre posizioni teoriche aperte (dal registro e dal ritracciamento), ogni nuovo segnale lo dice:

```
📂 Già aperti (teorici): ORB S&P500 LONG · Ritracciamento S&P500 LONG
💼 Con questo: 3 posizioni ≈ 4,5% di rischio (limite 4%) — ⚠️ oltre il limite: salta il segnale o riduci la size
🔗 Stessa direzione sull'azionario USA: ORB S&P500, Ritracciamento S&P500 → si muovono insieme, rischio concentrato
```

- Il calcolo usa il rischio per trade e il limite totale delle variabili `RISCHIO_PER_TRADE` (default 1,5%) e `MAX_RISCHIO_TOTALE` (default 4%).
- Gruppi che si muovono insieme: **oro** (VWAP, London Sweep, Kumo Oro) e **azionario USA** (ORB, Kumo Nasdaq, ritracciamento).
- Sono posizioni **teoriche**: il bot non sa quali hai aperto davvero. Una posizione che ha appena toccato lo stop può risultare ancora aperta per al massimo 30 minuti.

---

## 🚨 Avvisi automatici

Il bot ti scrive su Telegram quando:
- una strategia va in errore per **3 controlli di fila** (e di nuovo quando torna a funzionare);
- **Yahoo non restituisce dati** per uno strumento da più di 45 minuti (e quando tornano);
- **i dati sono fermi**: Yahoo risponde ma l'ultima candela non avanza da più di 2 ore di mercato aperto (e quando riprendono). Orari usati: future (oro) da domenica 18:00 a venerdì 17:00 di New York con pausa 17–18; indici e azioni 09:30–16:00 di New York, con festività e mezze sedute del calendario. Non conosce gli orari ridotti delle festività USA dei future: in quei giorni l'avviso può arrivare anche se la borsa è legittimamente chiusa;
- il **calendario notizie** sta per finire: dai 60 giorni prima dell'ultima data, al massimo una volta a settimana;
- lo **scheduler si blocca**: se per 20 minuti non dà segni di vita il bot si riavvia da solo (Railway lo riavvia) e il controllo di salute risponde `503` invece di `OK`.

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
📏 Distanze dall'entry: stop -125.30 | TP1 +187.95 | target +313.25
━━━━━━━━━━━━━━━━━━━━
⏱ SLITTAMENTO: +17.04 pt (+0.23%) — 🟡 Slittamento moderato
Prezzo ora 7295.00 vs entry 7277.96 (14% del rischio contro di te)
📐 R:R entrando ora: 1:2.1 (stop e target del segnale)
🔁 Stesse distanze dal prezzo ora → Stop 7169.70 | TP1 7482.95 | Target 7608.25
ℹ️ Prezzo Yahoo: confrontalo con quello su Revolut
━━━━━━━━━━━━━━━━━━━━
📝 Breakout OR high 7260.17 | Sopra VWAP | TP1 7465.91 → sposta SL a BE | Chiudi entro 21:45
⏱ INTRADAY — chiudi entro le 21:45 (ora italiana)
⚠️ Rischia max 1.5% capitale
🕐 Inviato 23/11 16:32
```

### Come si legge

- **Candela**: la candela su cui il bot ha deciso. L'entry è la chiusura di quella candela.
- **Entry / Stop / Target**: livelli teorici calcolati sui dati di Yahoo Finance.
- **R:R**: rapporto rischio/rendimento del segnale. È un testo calcolato con stop e target originali.
- **Distanze dall'entry**: stop e target come differenza dal prezzo d'ingresso. Applicale al prezzo che vedi su Revolut quando i prezzi non coincidono.
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

### ⚠️ Oro: prezzi del future, non dello spot

Le strategie sull'oro (1, 2, 4) leggono il **future COMEX** (`GC=F`), mentre il CFD oro di Revolut segue lo **spot** (XAU/USD). I due prezzi differiscono di solito di **decine di dollari**, quindi entry, stop e target del messaggio **non sono i livelli da inserire su Revolut**. I messaggi sull'oro lo segnalano: usa la riga **Distanze dall'entry** e applicala al tuo prezzo d'ingresso su Revolut (es. LONG entrato a 2620,50 con distanza stop −11,60 → stop a 2608,90). Le distanze sono quasi identiche tra future e spot.

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
python3 backtest.py azioni # ORB su CAT e AVGO (60 giorni, un trade al giorno)
python3 backtest.py trend  # ritracciamento nel trend, S&P500 giornaliero (dal 1993)
python3 backtest.py trend_azioni  # stessa idea su CAT e AVGO (giornaliero)
python3 backtest.py bot    # strategie 1, 2, 4, 5 del bot: VWAP Reversal, London Sweep, Kumo Oro 4H e Nasdaq 1H
python3 verifica_backtest.py  # controlla che bot e backtest diano gli stessi segnali (le 6 strategie accese)
COSTI_X=2 python3 backtest.py orb # stress test: spread moltiplicati per 2
```


Il calcolo del **Profit Factor** usa tutti i trade, anche quelli chiusi a fine sessione senza toccare stop né target ("Open"), al netto dei costi. La colonna `Open` ti dice quanti sono.

**Limiti da tenere presenti:**
- Per i dati a 15 minuti Yahoo Finance fornisce solo circa 60 giorni: l'ORB viene testato su poche decine di trade, troppo poche per un PF affidabile.
- Backtest `bot` (eseguito il 27/11/2026, costi Revolut attuali): **VWAP Reversal Oro** PF 1,71 su 10 trade (60 giorni); **London Sweep Oro** PF 0,69 su 8 trade (60 giorni); **Kumo Rider Oro 4H** PF 0,83 su 10 trade (2 anni); **Kumo Rider Nasdaq 1H** PF 0,00 su 5 trade (2 anni, tutti in perdita). Con costi raddoppiati (`COSTI_X=2`) i risultati cambiano poco. Campioni minuscoli: **nessuna delle quattro dimostra un vantaggio**, e tre su quattro sono sotto 1. I PF scritti in passato (1.84, 1.55, 1.62, 1.51) non sono confermati.
  - Semplificazioni: London Sweep senza TP1 al 50% e trailing; Kumo senza commissione notturna e con uscita forzata dopo circa 10 giorni; se nella stessa candela si toccano stop e target conta lo stop.
  - Il backtest Kumo usa la regola chikou standard (chiusura attuale contro quella di 26 candele prima) sull'ultima candela chiusa, come il bot dopo la correzione del 27/11/2026.
  - **Bot e backtest coincidono**: `python3 verifica_backtest.py` riesegue le funzioni del bot su ogni controllo storico (stessi orari dello scheduler, stessi dati che Yahoo dava in quel momento, stesse pause tra segnali) e le confronta con il backtest. Al 27/11/2026: stessi segnali e stessi prezzi per **tutte e sei le strategie accese** (VWAP 10/10, London 8/8, Kumo Oro 10/10, Kumo Nasdaq 5/5, ORB S&P500 57/57, Ritracciamento S&P500 152/152 eventi in 4 anni). Con i controlli allineati alle candele ogni segnale arriva 2 minuti dopo la chiusura della sua candela.
- I backtest non gestiscono il TP1 con break-even dell'ORB.
- **ORB BASE** (`backtest.py orb`) riproduce il bot dal 27/11/2026: opening range alle 09:30 di New York, candele 09:45–15:15 ET, filtro sull'ampiezza con le sole candele già disponibili, un LONG e uno SHORT al giorno al massimo, chiusura alle 15:45 ET. Le varianti sperimentali (Liquidity Grab, Initial Balance, Gap Fill, Regime) e il backtest `azioni` usano ancora l'orario italiano.
- Nessun backtest include il filtro notizie.
- **Costi**: spread in % del valore della posizione, ricavati dalla schermata d'ordine del CFD S&P 500 (≈0,019% andata e ritorno) e dai KID Revolut del 28/04/2026 (azioni ≈0,039%, materie prime ≈0,038%, indici ≈0,004%). Commissione notturna long = valore × (tasso base ≈3,8% + mark-up 3%) / 365, cioè ≈6,8% annuo, usata nei backtest giornalieri. **Oro** (schermata ordine XAU:CFD): spread ≈0,008% andata e ritorno, commissione notturna long ≈0,015% al giorno (≈5,3% annuo), margine 5%. Spread e tasso variano per orario e strumento: ricontrollali nell'app.
- Backtest `trend_azioni` (giornaliero, costi attuali: spread 0,04% e finanziamento 6,8% annuo): **CAT** non mostra vantaggio (PF 0,90 sviluppo e 0,92 verifica, sotto 1 anche con soglie diverse). **AVGO** sì: PF 1,39 sviluppo, 1,77 verifica, 270 trade dal 2010, e resta a 1,26 con costi tripli. Ma il rendimento è piccolo (circa +17% in 16 anni, ≈1% l'anno a rischio 1% per trade, in mercato per circa il 15% dei giorni) e funziona anche senza filtro sulla media a 200 giorni: gran parte del vantaggio può dipendere dal trend eccezionale del titolo (scelto col senno di poi) più che dalla regola. I gap sui risultati trimestrali sono nei dati ma non evitati.
- Backtest ORB (60 giorni, costi in % da schermata/KID): CAT PF 1,25 (36 trade), AVGO PF 1,30 (46 trade), S&P 500 **PF 1,40 (59 trade, +9,4%; 1,22 con costi raddoppiati)**, misurato il 27/11/2026 con il backtest corretto (sugli stessi dati la versione vecchia dava PF 1,00 su 48 trade: misurava una strategia diversa da quella del bot); le altre varianti S&P hanno 3–28 trade. Con campioni così piccoli e un solo periodo di mercato **non dimostrano un vantaggio**: l'ORB resta non validata. Circa due trade su tre (37 su 59 per l'S&P 500) chiudono a fine sessione senza toccare stop né target.
- Per ANT non esiste storico sufficiente: nessun backtest possibile.


### Test automatici

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests      # offline, dati simulati, pochi secondi
```

Su GitHub partono da soli a ogni pull request e a ogni push su `main` (`.github/workflows/test.yml`): installano le versioni bloccate con Python 3.11, come su Railway, e lanciano pyflakes e i test. Se una pull request mostra la ❌ rossa, non unirla prima di aver capito perché.

---

## 🧱 Limiti noti del bot

- **Dati Yahoo Finance**, non il prezzo del CFD di Revolut. Per l'oro è il future, con prezzi diversi dallo spot: usa le distanze.
- I controlli girano 2 minuti dopo la chiusura di ogni candela (:02, :17, :32, :47; Kumo Nasdaq ai :32). Se Yahoo pubblica i dati in ritardo (succede con i future come l'oro), il segnale può riferirsi alla candela precedente: il resoconto settimanale mostra il ritardo medio reale. Lo slittamento resta da controllare.
- Le varianti sperimentali dell'ORB e il backtest `azioni` usano ancora l'opening range delle 15:30 italiane: nelle settimane con ora legale sfasata simulano la candela sbagliata.
- Festivi e mezze sedute di Wall Street sono bloccati dal filtro notizie solo se sono nel calendario di `news_filter.py` (oggi fino a fine 2027). Le strategie su azioni (6, 7, 8) controllano anche che la candela sia recente; le altre no, quindi con una data mancante possono proporre segnali su candele vecchie. Ignorali.
- Il filtro notizie dipende da un calendario che aggiorni tu e non copre le notizie impreviste (vedi sezione dedicata).
- Non segue le tue operazioni reali: non ti avvisa del TP1 o della chiusura serale. Il registro dei segnali segue solo una posizione **teorica** per misurare le strategie.
- Il limite "max 4% totale" nel messaggio di avvio è solo un promemoria.

---

## 🚀 Deploy su Railway — Istruzioni passo passo

### STEP 1 — Carica il codice su GitHub

1. Vai su [github.com](https://github.com) → **New repository**
2. Dagli un nome (es. `trading-bot`) → **Create repository**
3. Carica tutti i file: `main.py`, `strategies.py`, `notifier.py`, `news_filter.py`, `stato.py`, `registro.py`, `backtest.py`, `verifica_backtest.py`, `requirements.txt`, `Procfile`, `railway.toml`
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

Variabili **opzionali** (strategie su azioni):

- `ANT_ABILITA_SHORT`, `CAT_ABILITA_SHORT`, `AVGO_ABILITA_SHORT` = `1` o `0` (default: ANT `0`, CAT e AVGO `1`)
- `TICKER_ANT`, `TICKER_CAT`, `TICKER_AVGO` = simbolo Yahoo Finance, se diverso da quello predefinito
- `RISCHIO_PER_TRADE`, `MAX_RISCHIO_TOTALE` = rischio per segnale e limite totale in % del capitale, usati per il calcolo "Già aperti" nei messaggi (default 1,5 e 4)
- `STRATEGIE_OMBRA` = strategie in modalità **solo registro** (es. `orb_cat,orb_avgo`): girano e finiscono nel resoconto del sabato, ma non mandano segnali. Il messaggio di avvio le elenca con 👻
- `STRATEGIE_SPENTE` = elenco di strategie da **spegnere**, separate da virgole (es. `orb_cat,orb_avgo`). Nomi validi: `vwap_gold`, `london_sweep`, `orb_sp500`, `orb_ant`, `orb_cat`, `orb_avgo`, `kumo_gold_4h`, `kumo_nasdaq`, `pullback_sp500`, `pullback_avgo`. Variabile assente o vuota = tutte attive. Per riaccenderle togli il nome: Railway riavvia il bot da solo. Il messaggio di avvio su Telegram mostra quali sono spente.

Railway riavvia il bot da solo.

### STEP 4b — Volume per la memoria del bot

Il bot salva in un file la pausa tra segnali e le posizioni teoriche del ritracciamento. Perché il file sopravviva ai deploy serve un **volume**:

1. Nel progetto Railway, clic destro sul servizio (o **⌘K → Volume**) → **Add Volume**
2. **Mount path**: `/data`
3. Railway riavvia il bot: il messaggio di avvio deve dire "💾 Memoria ripristinata" e non "⚠️ Nessun volume /data"

Senza volume il bot funziona lo stesso, ma la memoria si perde a ogni deploy. Per un percorso diverso imposta la variabile `STATO_PATH`.

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
