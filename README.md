# Control Room – app operatore

Interfaccia a pulsanti sopra GLPI 11 (REST API legacy). Due schermate:

- **Allarmi**: ticket "ALERT ON" aperti dell'entità Control Room in una **tabella** filtrabile (testo libero,
  acquedotto, tipo, solo ripetuti) e ordinabile per colonna. Un gruppo = stesso **sito + tipo di allarme +
  oggetto interessato**; se il corpo del ticket non indica né tipo né oggetto, discrimina la descrizione.
  Tipo, oggetto e descrizione si leggono dal corpo del ticket (opz. 21) cercando righe `Chiave: valore`
  (`Tipo allarme`/`Tipo`/`Allarme`…, `Oggetto`/`Apparato`/`Dispositivo`…, `Descrizione`/`Messaggio`…, vedi
  `app/parsing.py:_KEYS`); in mancanza, descrizione = prima riga del testo. I gruppi con più ticket si espandono
  (clic sulla riga) e mostrano i singoli ticket con link a GLPI. **Rientrato** / **Manovra** (nota obbligatoria)
  chiudono l'intero gruppo o il singolo ticket. I titoli non riconosciuti non si raggruppano mai.
  **Manovra senza chiusura** (dopo la presa in carico, che resta invariata): dal pannello Manovra, *Salva senza chiudere* aggiunge un followup GLPI (storico) e lascia
  il ticket in coda; *Salva e metti in attesa* porta il ticket allo stato GLPI "In attesa" (`GLPI_STATUS_WAITING`, 4),
  *Salva e riprendi* lo riporta a `GLPI_STATUS_RESUME` (2). Il pannello mostra lo storico manovre (followup) dei ticket;
  la coda evidenzia i ticket in attesa (filtro *Solo attivi* / *Solo in attesa*). I ticket in attesa restano chiudibili.
- **Nuova chiamata**: sito (ricerca tra le Posizioni GLPI) → chiamante (ricerca tra gli Utenti GLPI attivi, impostato
  come richiedente del ticket) → descrizione. Titolo `CHIAMATA: <codice> – <prima riga della descrizione>`,
  origine Telefono, posizione valorizzata. L'account di servizio GLPI deve avere lettura sugli Utenti.

Login con account AD: **bind LDAP con le credenziali dell'utente stesso** (UPN `utente@dominio`), poi, sulla stessa
connessione, lettura del suo `displayName` e verifica dei gruppi. **Nessun account di servizio**: non c'è un segreto LDAP
da custodire e il portale non può leggere AD più di quanto possa l'utente che fa login (gestione-contratti invece usa
`svc_contratti` perché il suo modulo cerca prima l'utente e poi verifica la password). Il certificato del DC è
sempre verificato (`LDAP_CA_CERT_FILE`, CA interna). **Ruoli** dai gruppi AD:
- `LDAP_OPERATOR_GROUP_DN` → **operatore**: Allarmi + Nuova chiamata (unico ruolo che scrive su GLPI).
- `LDAP_MANAGER_GROUP_DN` → **manager**: sezione Statistiche, sola lettura. `/api/resolve`, `/api/call`, `/api/queue`,
  `/api/sites` rispondono 403.
- In entrambi i gruppi → entrambe le viste. In nessuno → accesso negato. Un DN vuoto non assegna quel ruolo a nessuno.
- `LDAP_NESTED_GROUPS=true` (default) conta anche i gruppi annidati (`LDAP_MATCHING_RULE_IN_CHAIN`, specifico di AD,
  una query per ruolo); `false` usa solo `memberOf` diretto.
- `/grafana/*` è riservato ai manager: reverse proxy verso Grafana (vedi sotto).
- Sessioni emesse prima dell'introduzione dei ruoli non sono valide: serve un nuovo login. L'operatore reale è scritto nel testo di soluzione/ticket e in
un audit locale SQLite (`/api/audit`), perché le scritture su GLPI avvengono con l'account di servizio.

## Tema
Identità Siciliacque (font Maven Pro, loghi, blu istituzionale) dal repo `gestione-contratti`; linee guida in
`docs/design/`. Tema **scuro** (predefinito) e **chiaro**, con il pulsante ☀/☾ nell'intestazione; la scelta resta
nel browser. Grafana segue il tema del portale (parametro `theme`) e riceve font e colori da
`app/static/grafana-brand.css`, che il proxy aggiunge alle pagine HTML di Grafana. Quel CSS si appoggia a classi
interne di Grafana (verificato su 11.4.0): da ricontrollare quando si aggiorna l'immagine. Le etichette degli assi
dei grafici (disegnate su canvas) restano nel font di Grafana. I colori delle serie sono in `scripts/build_dashboards.py`.

## Setup in GLPI (da fare prima del primo avvio)
1. Abilita la REST API (Configurazione → Generale → API) e crea un **client API** con App-Token; limita per IP all'host dell'app.
2. Crea un utente di servizio `svc-controlroom` con **profilo dedicato** (solo entità 14, non ricorsivo): ticket vedere/creare/aggiornare,
   soluzioni aggiungere, posizioni lettura. Niente diritti di amministrazione. Genera il suo **user token** (API remote access).
3. Verifica gli ID in `.env`: entità (14), origine Telefono (8), tipi soluzione (1/2/3).
4. Verifica i campi di ricerca: `1`=titolo ticket / nome posizione, `12`=stato, `15`=data apertura, `80`=entità
   (`GET /apirest.php/listSearchOptions/Ticket` e `/Location`). Vedi "Cosa non è verificato".

## Architettura del compose
| Servizio | Reti | Porte pubblicate | Note |
|---|---|---|---|
| `app` | default, `grafana_net` (IP fisso 172.30.99.10) | `127.0.0.1:8000` | portale; nessuna dipendenza da Postgres/Grafana |
| `grafana` | `grafana_net` (172.30.99.11), `db_net` | nessuna | auth proxy accettato solo dall'IP dell'app; reti interne, niente Internet |
| `postgres` | `db_net` | nessuna | schema `reporting`; ruoli `cr_sync` (scrive) e `grafana_ro` (solo SELECT, sessioni read-only) |
| `sync` | default (GLPI), `db_net` | nessuna | stessa immagine, `python -m sync.worker`; audit dell'app montato in sola lettura |

**Statistiche (manager)**: il portale verifica il ruolo e inoltra `/grafana/*` a Grafana con `X-WEBAUTH-USER=<utente AD>`
(utente creato al primo accesso, ruolo **Viewer**). Il proxy scarta sempre `X-WEBAUTH-*`, `X-Grafana-*` e `Authorization`
inviati dal browser, non inoltra il cookie di sessione del portale, accetta solo GET/HEAD/POST e rifiuta l'username
`GRAFANA_ADMIN_USER`. L'iframe è sullo stesso origin del portale: `allow_embedding` è attivo ma il proxy impone
`frame-ancestors 'self'`, quindi nessun altro sito può incorniciare Grafana. Login form, basic auth, accesso anonimo e
Grafana Live (websocket) sono disattivati. L'admin locale di Grafana non ha modo di entrare (login form e basic auth spenti):
dashboard e datasource sono solo provisioning.

Se Grafana è giù la pagina Statistiche mostra un avviso entro ~3 s; coda alert e nuova chiamata non sono toccate.

## Reporting: sync GLPI → PostgreSQL
Il worker (`sync/worker.py`) gira ogni `SYNC_INTERVAL_SECONDS`:
1. legge da GLPI i ticket dell'entità con **data di ultima modifica** (opz. 19) successiva al cursore meno
   `SYNC_OVERLAP_SECONDS`, ordinati e paginati; al primo avvio carica tutto;
2. in una sola transazione fa l'upsert (rieseguibile senza effetti: idempotente), avanza il cursore e scrive
   `last_success_at`. **Se GLPI non risponde non tocca né dati né cursore**: registra solo `last_attempt_at` e
   `last_error`. Grafana mostra sempre i dati dell'ultima sync riuscita con il suo orario (`v_sync_status`);
3. ogni `SYNC_RECONCILE_HOURS` legge tutti gli ID e marca `deleted` i ticket spariti da GLPI (cestino/eliminati),
   che la ricerca per data di modifica non può vedere. Se GLPI restituisce 0 ticket la riconciliazione è saltata;
4. carica i CSV di `config/` se cambiati, applica le esclusioni, legge l'audit dell'app per "chi ha gestito".

Se Postgres è giù il worker riprova al giro successivo; il portale operatore non se ne accorge (non è sulla rete del DB).

### Regole KPI
Definite **una sola volta** nelle viste di `sync/schema.sql` (Grafana legge solo le viste `v_*`), ciascuna con test in
`tests/test_kpi.py`:

| # | Regola | Dove |
|---|---|---|
| R1 | Statistiche alert solo su titoli `ALERT ON:`; esclusi ALERT OFF, titoli che iniziano con "Prova", altri | `sync/rules.py:classify`, `v_alert` |
| R2 | Esclusi i ticket risolti **o** chiusi nei minuti `MASS_CLOSURE_MINUTES` (ora di Roma; default 20/08/2026 17:42), quelli in `config/excluded_tickets.csv` e i cancellati | `excluded_ticket`, `v_ticket_valid` |
| R3 | Tempo di gestione = `solvedate − date` (opz. 17 − 15), mai `closedate`; senza solvedate (o negativo) → escluso dalle statistiche dei tempi | `v_alert.ttr_min` |
| R4 | Ripetuto = alert sullo stesso codice sito entro `FLAPPING_WINDOW_MIN` minuti (**inclusi**) dal precedente alert valido; i titoli senza codice non sono mai ripetuti | `v_alert.is_repeat` |
| R5 | Ora, giorno e giorno della settimana dell'**apertura** in Europe/Rome; le date GLPI (ora locale senza fuso) sono convertite in UTC: nell'ora ripetuta di fine ottobre si assume l'ora legale | `sync/rules.py:parse_glpi_datetime`, `v_alert.hour_local` |
| R6 | Esito da tipo soluzione (opz. 23): contiene "rientrat" → Rientrato, "manovra" → Manovra, altro valore → Altro, vuoto → **Non classificato** | `v_alert.outcome` |
| R7 | Chi ha gestito = audit dell'app, altrimenti la riga `[Control Room app — operatore: …]` nel testo della soluzione (opz. 24), altrimenti "sconosciuto". "Ultima modifica di" non è mai usato | `handler`, `v_alert.handled_by` |

Nota: nella coda operatore "ripetuto" resta la regola dell'MVP (≥ `FLAPPING_THRESHOLD` ticket **aperti** sullo stesso
sito): è un indicatore operativo diverso da R4 e i due numeri non vanno confrontati.

### Mapping sito → reparto
Il reparto è il prefisso della Posizione GLPI (`SUD Blufi > ...` → `SUD Blufi`), presente solo sui ticket assegnati.
Il worker di sync valorizza la Posizione **a ogni giro, sugli alert aperti**, prima della presa in carico: se è
vuota, si cerca tra le Posizioni GLPI quella il cui nome contiene il codice sito (es. `FNC-IS00600`) come parola; con
una sola corrispondenza la si scrive sul ticket (`locations_id`, riga nel log del worker), con zero o più di una il
ticket resta com'è. Esito della ricerca in cache per codice (1 h se trovato, 5 min se no). Se alla presa in carico la
Posizione è ancora vuota, il portale apre subito un popup con il testo del ticket e la ricerca tra le Posizioni GLPI;
la scelta è scritta sul ticket (audit `set_location`). L'operatore può rimandare con "Più tardi".
Per un sito vale, nell'ordine: riga in `config/site_reparto.csv` (`codice_sito;reparto`, compilabile a mano), poi il
reparto più frequente nelle Posizioni dei suoi ticket, altrimenti "non mappato". Il CSV è ricaricato appena cambia;
se contiene righe non valide **non** viene caricato (resta il precedente) e l'errore finisce in
`sync_state.site_reparto_csv_error`. `v_mapping_coverage` dà la copertura (siti e alert mappati).

## Dashboard (manager → Statistiche)
Provisionate come codice, in sola lettura (`allowUiUpdates: false`), cartella "Control Room"; la Panoramica è la home.
Generate da `scripts/build_dashboards.py` → `grafana/dashboards/*.json` (modificare il builder e rigenerare:
`python -m scripts.build_dashboards`; un test fallisce se i JSON non sono allineati al builder).

| Dashboard | Contenuto |
|---|---|
| Panoramica | alert/giorno (con ripetuti), heatmap ora × giorno della settimana (ora di Roma), ticket aperti ora, età del backlog |
| Siti, acquedotti, reparti | Pareto top-20 siti con quota cumulata, trend settimanale per acquedotto, quota di ripetuti per sito, per acquedotto e per reparto, copertura del mapping |
| Tempi di gestione | mediana e p90 (solvedate − apertura) per ora di apertura, per acquedotto e per settimana; numero di esclusi senza solvedate |
| Esiti | Rientrato / Manovra / Altro / **Non classificato** per settimana e quota non classificata; esiti per operatore (solo chiusure tracciate) |
| Chiamate dal campo | volume settimanale, tempo mediano, aperte ora, per sito |

Ogni dashboard ha in testa **Fonte dati** (GLPI o SINTETICO), **ultima sincronizzazione riuscita** ed errore dell'ultimo
tentativo. Filtri: acquedotto (tutte), reparto (Siti). Le query leggono solo le viste `v_*`.

### Dataset sintetico (solo sviluppo)
`scripts/gen_synthetic.py` crea **4.194 ticket inventati** nel GLPI simulato e li passa al vero worker di sync.
Nell'intestazione del file sono separati i parametri presi dalle cifre note (totale, 194 ALERT OFF, ~800 con Posizione,
~50% ripetuti, 12 acquedotti, chiusura massiva 20/08 17:42, esiti solo dopo l'adozione) da quelli **inventati**
(periodo, siti "Sito NNN", reparti "SINT …", profilo orario, distribuzione dei tempi, quote degli esiti, operatori).
I numeri che produce **non dicono nulla sulla realtà**: servono a vedere che pannelli e regole funzionano.
Rifiuta di sovrascrivere un database con dati non sintetici (senza `--force`) e lascia l'etichetta "SINTETICO" visibile
su tutte le dashboard. Screenshot su questo dataset: `docs/screenshots/`.

    DATABASE_URL=postgresql://cr_sync:...@<host>:5432/controlroom python -m scripts.gen_synthetic --seed 42

## Deploy in produzione: `scripts/deploy.sh`
Stesse convenzioni di gestione-contratti (server Docker condiviso). Tutto sotto `/appdata/controlroom` (`CR_BASE`):

| Cartella | Contenuto | Permessi |
|---|---|---|
| `app/` | checkout git, **mai modificato a mano** (lo script rifiuta di procedere) | utente |
| `config/` | `.env` (600), `docker-compose.override.yml` generato, `certs/` (CA interna per LDAPS, 755) | 700 |
| `csv/` | `site_reparto.csv`, `excluded_tickets.csv` (modificabili; letti dal sync) | 755 |
| `data/` | `audit/` (uid 10001), `postgres/` (70, 700), `grafana/` (472) | |
| `backup/` | dump e archivi, anche automatici prima di ogni aggiornamento | 700 |

Progetto Compose fisso `controlroom`: lo script agisce solo sui suoi container e sulle sue immagini.
Nessun volume Docker nominato. Si installano **versioni** (tag git), non branch.

### 1. Deploy key (una volta)
Il server scarica il codice con una chiave SSH dedicata, **in sola lettura e solo su questo repository**
(revocabile da GitHub in un clic). Sul server:

    ssh-keygen -t ed25519 -N "" -C "deploy controlroom" -f ~/.ssh/controlroom_deploy && cat ~/.ssh/controlroom_deploy.pub

Su GitHub: ControlRoom → Settings → Deploy keys → Add deploy key, **senza** "Allow write access". Poi:

    cat >> ~/.ssh/config <<'FINE'
    Host github-controlroom
      HostName github.com
      User git
      IdentityFile ~/.ssh/controlroom_deploy
      IdentitiesOnly yes
    FINE
    ssh -T github-controlroom    # atteso: "Hi pietro95/ControlRoom! You've successfully authenticated"

### 2. Installazione
Copiare `scripts/deploy.sh` sul server solo per il primo `prepare` (poi si usa quello in `app/`):

    bash deploy.sh prepare                      # cartelle e permessi, clone, CSV, config/.env con segreti generati
    nano /appdata/controlroom/config/.env       # vedi sotto
    /appdata/controlroom/app/scripts/deploy.sh install v1.0.0
    /appdata/controlroom/app/scripts/deploy.sh check-glpi
    /appdata/controlroom/app/scripts/deploy.sh sync

**CA interna per LDAPS**: `prepare` crea `config/certs/` e, se esiste, copia `ca-interna.pem` di gestione-contratti
(la stessa CA firma il DC). Altrimenti va copiato a mano il certificato PEM della CA in `config/certs/ca-interna.pem`.
Lo script si ferma se il file indicato da `LDAP_CA_CERT_FILE` manca o se `LDAP_VALIDATE_TLS=false`; l'app stessa
non parte se il file configurato non esiste.

Da compilare in `config/.env`: `APP_PORT` (porta libera e non standard, lo script verifica che lo sia), `APP_BIND`
(con Nginx Proxy Manager in container: l'IP con cui il container raggiunge l'host, di solito `172.17.0.1`),
token GLPI, `LDAP_URL` e DN dei gruppi, `PORTAL_PUBLIC_URL` (`https://<nome DNS>`). **Attenzione**: `LDAP_*` e
`PORTAL_PUBLIC_URL` hanno valori d'esempio, quindi non risultano "vuoti": vanno controllati a mano.

`install <versione>`: backup se già installato → checkout del tag → controlli → build `controlroom:<versione>` →
avvio → attesa di salute e di `/healthz` su `APP_BIND:APP_PORT` → pulizia delle immagini vecchie del progetto.
Se il sync era attivo viene riavviato con la nuova immagine (salvo configurazione GLPI cambiata dopo `check-glpi`).
Altri comandi: `status`, `backup [etichetta]`. Rollback: `install <tag precedente>` (+ ripristino del dump se serve).

Lo script si ferma su: `.env` incompleto, `DEV_AUTH`/`DEV_ROLES` attivi, `COOKIE_SECURE` spento, `LDAP_VALIDATE_TLS`
spento, CA interna mancante, password del sync non
alfanumerica, Compose < 2.24, porta occupata, subnet `172.30.99.0/28` in uso, modifiche locali in `app/`, `sync`
senza `check-glpi` riuscito o con impostazioni GLPI cambiate dopo il check.

### 3. Nginx Proxy Manager
Proxy Host → Domain `<nome DNS del portale>`, Scheme `http`, Forward Hostname/IP = `APP_BIND`, Forward Port =
`APP_PORT`, "Block Common Exploits"; scheda SSL: certificato e "Force SSL". Grafana non va pubblicato: passa dal
portale sotto `/grafana/`. Verifica dal server: `curl -H "Host: <nome DNS>" http://<APP_BIND>:<APP_PORT>/healthz`.

### Rilascio di una versione
Tag annotato sul commit di `main` approvato, mai spostato o riutilizzato: `git tag -a v1.0.0 -m "..." && git push origin v1.0.0`.

## Avvio manuale (sviluppo)
    cp .env.example .env   # compila tutto (password: openssl rand -hex 24)
    docker compose up -d --build
I ruoli Postgres sono creati da `db/init/01_roles.sh` **solo alla prima inizializzazione** del volume `pgdata`: cambiare
in seguito `SYNC_DB_PASSWORD`/`GRAFANA_DB_PASSWORD` richiede `ALTER ROLE ... PASSWORD` a mano.
L'audit SQLite sta nel volume nominato `appdata` (prima era il bind mount `./data`, che il container non poteva scrivere
perché creato da Docker come root: il login andava in errore 500).
Metti davanti un reverse proxy con TLS (cookie `Secure`, `SameSite=Strict`). Rete: solo LAN Control Room.

## Sviluppo / test
    pip install -r requirements-dev.txt
    pytest                                   # GLPI e LDAP simulati in-process, PostgreSQL locale temporaneo
    uvicorn mock.mock_glpi:app --port 9000   # GLPI finto
    GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 \
      uvicorn app.main:create_app --factory --port 8000    # login: qualunque utente, password "dev"
`DEV_AUTH=1` disattiva l'AD: **mai in produzione**. Ruoli in sviluppo: `DEV_ROLES="capo:manager,both:operator+manager"`
(gli utenti non elencati sono operatori).

## Cosa è verificato (fase 2)
In questo ambiente con Docker reale (Grafana OSS 11.4.0, Postgres 16), con `DEV_AUTH=1` e GLPI non raggiungibile:
- tutti i servizi diventano `healthy`; Grafana e Postgres non sono raggiungibili dall'host;
- un altro container su `grafana_net` che invia `X-WEBAUTH-USER: cr-grafana-admin` riceve 401 (whitelist IP);
- dal portale, il manager entra in Grafana come sé stesso, Viewer, non admin, anche inviando un `X-WEBAUTH-USER` falso;
  la creazione di dashboard è negata; l'operatore riceve 403;
- il datasource si connette come `grafana_ro` e ogni `CREATE TABLE` (in `reporting` e `public`) fallisce;
- con Grafana fermo: `/healthz`, `/api/me` e la pagina coda rispondono 200, `/stats` mostra l'avviso.

## Cosa è verificato (fase 3)
- 79 test pytest. Le regole KPI e il worker girano su un **PostgreSQL 16 reale** (cluster temporaneo creato dai test
  con lo stesso `db/init/roles.psql` del container; serve PostgreSQL installato o `PG_BIN`). Prova di robustezza dei
  test: rompendo apposta tre regole (finestra `<` invece di `<=`, closedate invece di solvedate, ora UTC invece di Roma)
  i test falliscono.
- Stack Docker completo con GLPI simulato: carico iniziale, chiusura dal portale → audit letto in sola lettura dal sync →
  `v_alert` con esito e operatore, letta da Grafana come `grafana_ro`; GLPI fermo → dati e `last_success_at` invariati,
  errore registrato; Postgres fermo → coda alert 200, il sync si riconnette da solo.

## Cosa è verificato (fase 4)
- `tests/test_dashboards.py`: JSON allineati al builder; struttura (uid unici, fuso Europe/Rome, riga di freschezza,
  nessun uso di closedate, solo viste `v_*`); **ogni query di ogni pannello e variabile eseguita come `grafana_ro`** sul
  dataset sintetico caricato tramite il worker, senza errori e senza risultati vuoti; heatmap sempre con 24 ore; il
  dataset rispetta i totali dichiarati; il generatore è deterministico e non sovrascrive dati reali.
- Grafana 11.4 reale nel compose, attraverso il proxy del portale come manager: 5 dashboard caricate dal provisioning,
  0 errori di query, 0 pannelli "No data" sul periodo del dataset, Panoramica come home di `/stats`.
  Durante questa verifica sono emersi e sono stati corretti: stat dell'ultima sync vuoto (Grafana non mostra timestamp
  nei pannelli stat: ora epoch ms), colori-soglia di default fuorvianti, etichette orarie lette come numeri.

## Cosa non è verificato
- **Mai provato contro il GLPI reale** (dal sandbox di sviluppo non era raggiungibile): flusso, parametri di ricerca, e
  forma delle risposte seguono la documentazione dell'API legacy e sono testati solo sul mock. Primo test: ambiente
  di prova o ticket di prova, con `DEV_AUTH` spento.
- **LDAPS con la CA interna**: la verifica del certificato è testata con un vero handshake TLS contro un server di prova
  (`tests/test_ldap_tls.py`: accettato con la CA giusta, rifiutato senza o con un'altra CA). Da verificare col DC reale:
  che `ca-interna.pem` sia la CA che lo firma e che il nome in `LDAP_URL` compaia nel certificato del DC.
  Attenzione: Python 3.13+ rifiuta CA prive dell'estensione *keyUsage*; l'immagine usa 3.12, ma prima di aggiornarla
  verificare la CA interna (`openssl x509 -in ca-interna.pem -noout -ext keyUsage`).
- **Login AD e ruoli**: bind UPN, query `memberOf:1.2.840.113556.1.4.1941:=<DN>` per i gruppi annidati e mapping
  ruoli testati solo con una connessione LDAP simulata (`tests/test_roles.py`). Da verificare su DC reale: che l'account
  in un gruppo annidato ottenga il ruolo, che i DN in `.env` coincidano esattamente con quelli dei gruppi, e i tempi
  della query IN_CHAIN (su alberi grandi può essere lenta).
- Il filtro stato `notold` e la ricerca posizioni per testo (campo 1) vanno confermati sul vostro GLPI.
- **Sync, da verificare sul GLPI reale prima di fidarsi dei numeri**:
  - gli ID delle search option (`GLPI_SO_*` in `.env`): 19/17/16/15 sono quelli standard, **23 (tipo soluzione), 24 (testo
    soluzione), 9 (origine) e 83 (posizione) sono i più incerti**: `GET /apirest.php/listSearchOptions/Ticket`;
  - che il criterio `morethan` sulla data di modifica funzioni e che le date siano nel fuso Europe/Rome del server;
  - il formato dei valori restituiti (stato numerico o testo, posizione con separatore `>`, testo soluzione HTML-escaped);
  - il numero di ticket esclusi dalla chiusura massiva del 20/08/2026 17:42: controllarlo a mano
    (`SELECT count(*) FROM reporting.excluded_ticket WHERE reason LIKE 'chiusura%'`); se il criterio prende troppo o troppo
    poco, usare `config/excluded_tickets.csv` e svuotare `MASS_CLOSURE_MINUTES`;
  - i volumi: carico iniziale di ~4.200 ticket a pagine da 500.

## Ripiego
Se GLPI non risponde, l'app mostra un banner con il link a GLPI. Se cade l'app, resta il modulo GLPI nativo:
nessuna procedura dipende solo dall'app.

## Limiti noti (MVP)
- Chiusura di gruppo: "Rientrato" chiude tutti i ticket aperti del sito, anche quelli di ore prima.
- Storico manovre: solo i followup GLPI (anche quelli scritti a mano in GLPI); max 20 ticket per gruppo nel pannello.
- Sync/KPI: un ticket "In attesa" conta come aperto (stato < 5); il tempo in attesa non è scorporato dai tempi di risoluzione.
- Nessun annulla dopo la chiusura; nessuna paginazione oltre 1000 ticket aperti nella coda.
- Dashboard: la tabella Pareto (5 colonne) scorre in orizzontale sotto i ~1600 px di larghezza; nessun alert Grafana configurato.
- Sessione 12 h; rate limit login in memoria (si azzera al riavvio).
