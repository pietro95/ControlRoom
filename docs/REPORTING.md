# Reporting — sync, regole KPI e dashboard

Documento autorevole per come si calcolano i numeri mostrati ai manager.

## 1. Sync GLPI → PostgreSQL

Il worker (`sync/worker.py`, servizio `sync`) gira ogni `SYNC_INTERVAL_SECONDS`:

1. legge da GLPI i ticket dell'entità con **data di ultima modifica** (opz. 19) successiva al cursore meno
   `SYNC_OVERLAP_SECONDS`, ordinati e paginati; al primo avvio carica tutto;
2. in una sola transazione fa l'upsert (idempotente), avanza il cursore e scrive `last_success_at`.
   **Se GLPI non risponde non tocca né dati né cursore**: registra solo `last_attempt_at` e `last_error`. Grafana
   mostra sempre i dati dell'ultima sync riuscita con il suo orario (`v_sync_status`);
3. ogni `SYNC_RECONCILE_HOURS` legge tutti gli ID e marca `deleted` i ticket spariti da GLPI (cestino/eliminati), che
   la ricerca per data di modifica non può vedere. Se GLPI restituisce 0 ticket la riconciliazione è saltata;
4. carica i CSV di `config/` se cambiati, applica le esclusioni, legge l'audit dell'app per "chi ha gestito".

Se Postgres è giù il worker riprova al giro successivo; il portale non se ne accorge (non è sulla rete del DB).

Le search option GLPI usate sono configurabili (`GLPI_SO_*`) e vanno verificate con `scripts/deploy.sh check-glpi`
prima di avviare il sync (vedi [RUNBOOK](RUNBOOK.md)).

## 2. Regole KPI

Definite **una sola volta** nelle viste di `sync/schema.sql` (Grafana legge solo le viste `v_*`), ciascuna con test in
`tests/test_kpi.py`.

| # | Regola | Dove |
|---|---|---|
| R1 | Statistiche alert solo su titoli `ALERT ON:`; esclusi ALERT OFF, titoli che iniziano con "Prova", altri | `sync/rules.py:classify`, `v_alert` |
| R2 | Esclusi i ticket risolti **o** chiusi nei minuti `MASS_CLOSURE_MINUTES` (ora di Roma; default 20/08/2026 17:42), quelli in `config/excluded_tickets.csv` e i cancellati | `excluded_ticket`, `v_ticket_valid` |
| R3 | Tempo di gestione = `solvedate − date` (opz. 17 − 15), mai `closedate`; senza solvedate (o negativo) → escluso dalle statistiche dei tempi | `v_alert.ttr_min` |
| R4 | Ripetuto = alert sullo stesso codice sito entro `FLAPPING_WINDOW_MIN` minuti (**inclusi**) dal precedente alert valido; i titoli senza codice non sono mai ripetuti | `v_alert.is_repeat` |
| R5 | Ora, giorno e giorno della settimana dell'**apertura** in Europe/Rome; le date GLPI (ora locale senza fuso) sono convertite in UTC; nell'ora ripetuta di fine ottobre si assume l'ora legale | `sync/rules.py:parse_glpi_datetime`, `v_alert.hour_local` |
| R6 | Esito da tipo soluzione (opz. 23): contiene "rientrat" → Rientrato, "manovra" → Manovra, altro valore → Altro, vuoto → **Non classificato** | `v_alert.outcome` |
| R7 | Chi ha gestito = audit dell'app, altrimenti la riga `[Control Room app — operatore: …]` nel testo della soluzione (opz. 24), altrimenti "sconosciuto". "Ultima modifica di" non è mai usato | `handler`, `v_alert.handled_by` |

Il "ripetuto" della coda operatore è una regola diversa (vedi [SPEC §2](SPEC.md#2-allarmi-operatore)).

## 3. Mapping sito → reparto

Il reparto è il prefisso della Posizione GLPI (`SUD Blufi > ...` → `SUD Blufi`), presente solo sui ticket assegnati.

**Valorizzazione automatica della Posizione.** A ogni giro, sugli alert aperti senza Posizione, il worker cerca tra
le Posizioni GLPI quella il cui nome contiene il codice sito (es. `FNC-IS00600`) come parola: con una sola
corrispondenza la scrive sul ticket (`locations_id`, riga nel log del worker); con zero o più di una il ticket resta
com'è. Esito in cache per codice (1 h se trovato, 5 min se no). Se manca ancora alla presa in carico, interviene il
popup del portale ([SPEC §2](SPEC.md#2-allarmi-operatore)).

**Reparto di un sito**, nell'ordine:

1. riga in `config/site_reparto.csv` (`codice_sito;reparto`, compilabile a mano);
2. reparto più frequente nelle Posizioni dei suoi ticket;
3. "non mappato".

Il CSV è ricaricato appena cambia; se contiene righe non valide **non** viene caricato (resta il precedente) e
l'errore finisce in `sync_state.site_reparto_csv_error`. `v_mapping_coverage` dà la copertura (siti e alert mappati).

## 4. Dashboard

Provisionate come codice, in sola lettura (`allowUiUpdates: false`), cartella "Control Room"; la Panoramica è la home.
Generate da `scripts/build_dashboards.py` → `grafana/dashboards/*.json`: si modifica il builder e si rigenera con
`python -m scripts.build_dashboards`.

| Dashboard | Contenuto |
|---|---|
| Panoramica | alert/giorno (con ripetuti), heatmap ora × giorno della settimana (ora di Roma), ticket aperti ora, età del backlog |
| Siti, acquedotti, reparti | Pareto top-20 siti con quota cumulata, trend settimanale per acquedotto, quota di ripetuti per sito, acquedotto e reparto, copertura del mapping |
| Tempi di gestione | mediana e p90 (solvedate − apertura) per ora di apertura, acquedotto e settimana; numero di esclusi senza solvedate |
| Esiti | Rientrato / Manovra / Altro / **Non classificato** per settimana e quota non classificata; esiti per operatore (solo chiusure tracciate) |
| Chiamate dal campo | volume settimanale, tempo mediano, aperte ora, per sito |

Ogni dashboard ha in testa **Fonte dati** (GLPI o SINTETICO), **ultima sincronizzazione riuscita** ed errore
dell'ultimo tentativo. Filtri: acquedotto (tutte), reparto (Siti). Le query leggono solo le viste `v_*`.

## 5. Dataset sintetico (solo sviluppo)

`scripts/gen_synthetic.py` crea **4.194 ticket inventati** nel GLPI simulato e li passa al vero worker di sync.
Nell'intestazione del file sono separati i parametri presi dalle cifre note (totale, 194 ALERT OFF, ~800 con
Posizione, ~50% ripetuti, 12 acquedotti, chiusura massiva 20/08 17:42, esiti solo dopo l'adozione) da quelli
**inventati** (periodo, siti "Sito NNN", reparti "SINT …", profilo orario, distribuzione dei tempi, quote degli esiti,
operatori). I numeri prodotti **non dicono nulla sulla realtà**: servono a vedere che pannelli e regole funzionano.

Rifiuta di sovrascrivere un database con dati non sintetici (salvo `--force`) e lascia l'etichetta "SINTETICO" su
tutte le dashboard. Screenshot su questo dataset: [`screenshots/`](screenshots/).

```bash
DATABASE_URL=postgresql://cr_sync:<password>@<host>:5432/controlroom python -m scripts.gen_synthetic --seed 42
```
