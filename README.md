# Control Room – app operatore

Interfaccia a pulsanti sopra GLPI 11 (REST API legacy). Due schermate:

- **Allarmi**: ticket "ALERT ON" aperti dell'entità Control Room, raggruppati per sito (un'unica card per sito,
  con contatore e marcatore "ripetuto"). Pulsanti **Rientrato** / **Manovra eseguita** (nota obbligatoria):
  chiudono con una soluzione strutturata *tutti* i ticket aperti del sito. I titoli non riconosciuti non si
  raggruppano mai (una card per ticket).
- **Nuova chiamata**: ricerca sito → tipo (pulsanti) → chiamante → ticket con titolo `CHIAMATA: <codice> – <tipo>`,
  origine Telefono, posizione e urgenza valorizzate.

Login con account AD (bind LDAP). **Ruoli** dai gruppi AD:
- `LDAP_OPERATOR_GROUP_DN` → **operatore**: Allarmi + Nuova chiamata (unico ruolo che scrive su GLPI).
- `LDAP_MANAGER_GROUP_DN` → **manager**: sezione Statistiche, sola lettura. `/api/resolve`, `/api/call`, `/api/queue`,
  `/api/sites` rispondono 403.
- In entrambi i gruppi → entrambe le viste. In nessuno → accesso negato. Un DN vuoto non assegna quel ruolo a nessuno.
- `LDAP_NESTED_GROUPS=true` (default) conta anche i gruppi annidati (`LDAP_MATCHING_RULE_IN_CHAIN`, specifico di AD,
  una query per ruolo); `false` usa solo `memberOf` diretto.
- `/grafana/*` è riservato ai manager: reverse proxy verso Grafana (vedi sotto).
- Sessioni emesse prima dell'introduzione dei ruoli non sono valide: serve un nuovo login. L'operatore reale è scritto nel testo di soluzione/ticket e in
un audit locale SQLite (`/api/audit`), perché le scritture su GLPI avvengono con l'account di servizio.

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
| `sync` | — | — | worker GLPI → Postgres, nella fase successiva |

**Statistiche (manager)**: il portale verifica il ruolo e inoltra `/grafana/*` a Grafana con `X-WEBAUTH-USER=<utente AD>`
(utente creato al primo accesso, ruolo **Viewer**). Il proxy scarta sempre `X-WEBAUTH-*`, `X-Grafana-*` e `Authorization`
inviati dal browser, non inoltra il cookie di sessione del portale, accetta solo GET/HEAD/POST e rifiuta l'username
`GRAFANA_ADMIN_USER`. L'iframe è sullo stesso origin del portale: `allow_embedding` è attivo ma il proxy impone
`frame-ancestors 'self'`, quindi nessun altro sito può incorniciare Grafana. Login form, basic auth, accesso anonimo e
Grafana Live (websocket) sono disattivati. L'admin locale di Grafana non ha modo di entrare (login form e basic auth spenti):
dashboard e datasource sono solo provisioning.

Se Grafana è giù la pagina Statistiche mostra un avviso entro ~3 s; coda alert e nuova chiamata non sono toccate.

## Avvio
    cp .env.example .env   # compila tutto (password: openssl rand -hex 24)
    docker compose up -d --build
I ruoli Postgres sono creati da `db/init/01_roles.sh` **solo alla prima inizializzazione** del volume `pgdata`: cambiare
in seguito `SYNC_DB_PASSWORD`/`GRAFANA_DB_PASSWORD` richiede `ALTER ROLE ... PASSWORD` a mano.
L'audit SQLite sta nel volume nominato `appdata` (prima era il bind mount `./data`, che il container non poteva scrivere
perché creato da Docker come root: il login andava in errore 500).
Metti davanti un reverse proxy con TLS (cookie `Secure`, `SameSite=Strict`). Rete: solo LAN Control Room.

## Sviluppo / test
    pip install -r requirements-dev.txt
    pytest                                   # GLPI e LDAP simulati in-process
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

## Cosa non è verificato
- **Mai provato contro il GLPI reale** (dal sandbox di sviluppo non era raggiungibile): flusso, parametri di ricerca, e
  forma delle risposte seguono la documentazione dell'API legacy e sono testati solo sul mock. Primo test: ambiente
  di prova o ticket di prova, con `DEV_AUTH` spento.
- **Login AD e ruoli**: bind UPN, query `memberOf:1.2.840.113556.1.4.1941:=<DN>` per i gruppi annidati e mapping
  ruoli testati solo con una connessione LDAP simulata (`tests/test_roles.py`). Da verificare su DC reale: che l'account
  in un gruppo annidato ottenga il ruolo, che i DN in `.env` coincidano esattamente con quelli dei gruppi, e i tempi
  della query IN_CHAIN (su alberi grandi può essere lenta).
- Il filtro stato `notold` e la ricerca posizioni per testo (campo 1) vanno confermati sul vostro GLPI.

## Ripiego
Se GLPI non risponde, l'app mostra un banner con il link a GLPI. Se cade l'app, resta il modulo GLPI nativo:
nessuna procedura dipende solo dall'app.

## Limiti noti (MVP)
- Chiusura di gruppo: "Rientrato" chiude tutti i ticket aperti del sito, anche quelli di ore prima.
- Nessuna dashboard manager (fase 3), nessun annulla dopo la chiusura, nessuna paginazione oltre 1000 ticket aperti.
- Sessione 12 h; rate limit login in memoria (si azzera al riavvio).
