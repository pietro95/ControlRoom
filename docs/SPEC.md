# Specifica funzionale — portale Control Room

Comportamento del portale operatore e regole di accesso. Le regole di calcolo delle statistiche sono in
[REPORTING](REPORTING.md).

## 1. Principi

- GLPI resta il sistema di registrazione: il portale legge e scrive ticket dell'entità Control Room tramite un
  account di servizio, non conserva dati propri salvo l'audit locale.
- Ripiego: se GLPI non risponde, l'app mostra un banner con il link a GLPI; se cade l'app, resta il modulo GLPI
  nativo. Nessuna procedura dipende solo dall'app.

## 2. Allarmi (operatore)

Ticket `ALERT ON` aperti dell'entità Control Room, in una **tabella** filtrabile (testo libero, acquedotto, tipo,
solo ripetuti) e ordinabile per colonna.

**Raggruppamento.** Un gruppo = stesso **sito + tipo di allarme + oggetto interessato**; se il corpo del ticket non
indica né tipo né oggetto, discrimina la descrizione. I titoli non riconosciuti non si raggruppano mai.

**Lettura del corpo** (opz. 21): righe `Chiave: valore` con chiavi `Tipo allarme`/`Tipo`/`Allarme`…,
`Oggetto`/`Apparato`/`Dispositivo`…, `Descrizione`/`Messaggio`… (elenco completo in `app/parsing.py:_KEYS`); in
mancanza, descrizione = prima riga del testo.

**Gruppi.** I gruppi con più ticket si espandono (clic sulla riga) e mostrano i singoli ticket con link a GLPI;
l'ID GLPI è mostrato anche per i ticket singoli.

**Chiusura.** **Rientrato** / **Manovra** (nota obbligatoria) chiudono l'intero gruppo o il singolo ticket.

**Manovra senza chiusura** (dopo la presa in carico, che resta invariata), dal pannello Manovra:

| Azione | Effetto in GLPI |
|---|---|
| *Salva senza chiudere* | followup (storico); il ticket resta in coda |
| *Salva e metti in attesa* | followup + stato `GLPI_STATUS_WAITING` (4, "In attesa") |
| *Salva e riprendi* | followup + stato `GLPI_STATUS_RESUME` (2) |

Il pannello mostra lo storico manovre (followup) dei ticket; la coda evidenzia i ticket in attesa (filtri
*Solo attivi* / *Solo in attesa*). I ticket in attesa restano chiudibili.

**Posizione mancante.** Se alla presa in carico la Posizione GLPI è vuota (il worker di sync prova a valorizzarla
prima, vedi [REPORTING §3](REPORTING.md#3-mapping-sito--reparto)), il portale apre un popup con il testo del ticket e la
ricerca tra le Posizioni GLPI; la scelta è scritta sul ticket (audit `set_location`). L'operatore può rimandare con
"Più tardi".

**Ripetuto in coda.** ≥ `FLAPPING_THRESHOLD` ticket **aperti** sullo stesso sito. È un indicatore operativo diverso
dalla regola KPI R4: i due numeri non vanno confrontati.

## 3. Nuova chiamata (operatore)

Sito (ricerca tra le Posizioni GLPI) → chiamante (ricerca tra gli Utenti GLPI attivi, impostato come richiedente)
→ descrizione. Titolo `CHIAMATA: <codice> – <prima riga della descrizione>`, origine Telefono, posizione valorizzata.

- **Apri ticket**: crea il ticket in stato Nuovo.
- **Salva e prendi in carico**: lo crea e assegna subito l'operatore come tecnico (stato In corso). Se l'operatore
  non è un utente GLPI attivo, la richiesta è rifiutata prima di creare il ticket; se fallisce solo la presa in
  carico, il ticket resta creato e l'errore è mostrato.

L'account di servizio GLPI deve avere lettura sugli Utenti.

## 4. Statistiche (manager)

Pagina `/stats` con le dashboard Grafana incorniciate dal portale (contenuto in
[REPORTING §4](REPORTING.md#4-dashboard)). Se Grafana è giù la pagina mostra un avviso entro ~3 s; coda allarmi e
nuova chiamata non ne risentono.

## 5. Accesso e ruoli

Login con account AD tramite **bind con le credenziali dell'utente** (UPN `utente@dominio`); sulla stessa connessione
si leggono `displayName` e gruppi. Ruoli:

| Gruppo | Ruolo | Accesso |
|---|---|---|
| `LDAP_OPERATOR_GROUP_DN` | operatore | Allarmi, Nuova chiamata |
| `LDAP_MANAGER_GROUP_DN` | manager | Statistiche e `/grafana/*`; `/api/resolve`, `/api/call`, `/api/queue`, `/api/sites` → 403 |
| entrambi | entrambi | tutte le viste |
| nessuno | — | accesso negato |

- Un DN vuoto non assegna quel ruolo a nessuno.
- `LDAP_NESTED_GROUPS=true` (default) conta anche i gruppi annidati (`LDAP_MATCHING_RULE_IN_CHAIN`, specifico di AD,
  una query per ruolo); `false` usa solo `memberOf` diretto.
- Sessioni emesse prima dell'introduzione dei ruoli non sono valide: serve un nuovo login.

**Tracciabilità.** Le scritture su GLPI avvengono con l'account di servizio, quindi l'operatore reale è scritto nel
testo di soluzione/ticket e in un audit locale SQLite (`/api/audit`).

## 6. Prestazioni

La coda è letta da GLPI una volta ogni `QUEUE_CACHE_SECONDS` (default 5) e condivisa fra operatori e azioni; ogni
scrittura dal portale la invalida. Ricerche alert/chiamate, risoluzione dei nomi dei tecnici, scritture su più ticket
e storico vanno a GLPI in parallelo (max `GLPI_CONCURRENCY`, default 6).

## 7. Tema

Identità Siciliacque (font Maven Pro, loghi, blu istituzionale) ripresa da gestione-contratti; linee guida in
[`design/`](design/). Tema **scuro** (predefinito) e **chiaro** con il pulsante ☀/☾; la scelta resta nel browser.
Grafana segue il tema del portale (parametro `theme`) e riceve font e colori da `app/static/grafana-brand.css`, che il
proxy aggiunge alle pagine HTML di Grafana. Quel CSS si appoggia a classi interne di Grafana (verificato su 11.4.0):
va ricontrollato a ogni aggiornamento dell'immagine. Le etichette degli assi (disegnate su canvas) restano nel font di
Grafana. I colori delle serie sono in `scripts/build_dashboards.py`.
