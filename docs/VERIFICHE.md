# Stato delle verifiche e limiti noti

Distingue ciò che è stato provato da ciò che è solo atteso. Aggiornare a ogni verifica sul campo.

## Cosa è verificato

### Fase 2 — isolamento di Grafana e del reporting

Docker reale (Grafana OSS 11.4.0, Postgres 16), con `DEV_AUTH=1` e GLPI non raggiungibile:

- tutti i servizi diventano `healthy`; Grafana e Postgres non sono raggiungibili dall'host;
- un altro container su `grafana_net` che invia `X-WEBAUTH-USER: cr-grafana-admin` riceve 401 (whitelist IP);
- dal portale il manager entra in Grafana come sé stesso, Viewer, non admin, anche inviando un `X-WEBAUTH-USER`
  falso; la creazione di dashboard è negata; l'operatore riceve 403;
- il datasource si connette come `grafana_ro` e ogni `CREATE TABLE` (in `reporting` e `public`) fallisce;
- con Grafana fermo: `/healthz`, `/api/me` e la pagina coda rispondono 200, `/stats` mostra l'avviso.

### Fase 3 — sync e regole KPI

- Regole KPI e worker testati su **PostgreSQL 16 reale** (cluster temporaneo creato dai test con lo stesso
  `db/init/roles.psql` del container). Prova di robustezza: rompendo apposta tre regole (finestra `<` invece di `<=`,
  closedate invece di solvedate, ora UTC invece di Roma) i test falliscono.
- Stack Docker completo con GLPI simulato: carico iniziale, chiusura dal portale → audit letto in sola lettura dal
  sync → `v_alert` con esito e operatore, letta da Grafana come `grafana_ro`; GLPI fermo → dati e `last_success_at`
  invariati, errore registrato; Postgres fermo → coda allarmi 200, il sync si riconnette da solo.

### Fase 4 — dashboard

- `tests/test_dashboards.py`: JSON allineati al builder; struttura (uid unici, fuso Europe/Rome, riga di freschezza,
  nessun uso di closedate, solo viste `v_*`); **ogni query di ogni pannello e variabile eseguita come `grafana_ro`**
  sul dataset sintetico caricato tramite il worker, senza errori e senza risultati vuoti; heatmap sempre con 24 ore;
  il dataset rispetta i totali dichiarati; il generatore è deterministico e non sovrascrive dati reali.
- Grafana 11.4 reale nel compose, attraverso il proxy come manager: 5 dashboard caricate, 0 errori di query,
  0 pannelli "No data" sul periodo del dataset, Panoramica come home di `/stats`.

## Cosa non è verificato

- **Mai provato contro il GLPI reale**: flusso, parametri di ricerca e forma delle risposte seguono la documentazione
  dell'API legacy e sono testati solo sul mock. Primo test: ambiente o ticket di prova, con `DEV_AUTH` spento.
- **LDAPS con la CA interna**: la verifica del certificato è testata con un vero handshake TLS contro un server di
  prova (`tests/test_ldap_tls.py`). Da verificare col DC reale: che `ca-interna.pem` sia la CA che lo firma e che il
  nome in `LDAP_URL` compaia nel certificato. Python 3.13+ rifiuta CA prive dell'estensione *keyUsage*: l'immagine usa
  3.12, ma prima di aggiornarla controllare
  `openssl x509 -in ca-interna.pem -noout -ext keyUsage`.
- **Login AD e ruoli**: bind UPN, query `memberOf:1.2.840.113556.1.4.1941:=<DN>` e mapping ruoli testati solo con una
  connessione LDAP simulata (`tests/test_roles.py`). Da verificare: ruolo ottenuto tramite gruppo annidato, DN in
  `.env` identici a quelli reali, tempi della query IN_CHAIN su alberi grandi.
- Il filtro stato `notold` e la ricerca posizioni per testo (campo 1) vanno confermati sul GLPI aziendale.
- **Sync, prima di fidarsi dei numeri**:
  - search option `GLPI_SO_*`: 19/17/16/15 sono standard, **23 (tipo soluzione), 24 (testo soluzione), 9 (origine) e
    83 (posizione) sono le più incerte** (`deploy.sh check-glpi`);
  - che il criterio `morethan` sulla data di modifica funzioni e che le date siano nel fuso Europe/Rome del server;
  - formato dei valori (stato numerico o testo, posizione con separatore `>`, testo soluzione HTML-escaped);
  - numero di ticket esclusi dalla chiusura massiva del 20/08/2026 17:42 (query in
    [RUNBOOK §7](RUNBOOK.md#7-dopo-il-primo-avvio));
  - volumi: carico iniziale di ~4.200 ticket a pagine da 500.

## Limiti noti

- Chiusura di gruppo: "Rientrato" chiude tutti i ticket aperti del gruppo, anche quelli di ore prima.
- Storico manovre: solo i followup GLPI (anche quelli scritti a mano in GLPI); max 20 ticket per gruppo nel pannello.
- Sync/KPI: un ticket "In attesa" conta come aperto (stato < 5); il tempo in attesa non è scorporato dai tempi.
- Nessun annulla dopo la chiusura; nessuna paginazione oltre 1000 ticket aperti nella coda.
- Dashboard: la tabella Pareto scorre in orizzontale sotto i ~1600 px; nessun alert Grafana configurato.
- Sessione 12 h; rate limit del login in memoria (si azzera al riavvio).
