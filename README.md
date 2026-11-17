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
- `/grafana/*` è riservato ai manager (il proxy verso Grafana arriva nella fase successiva; ora risponde 503).
- Sessioni emesse prima dell'introduzione dei ruoli non sono valide: serve un nuovo login. L'operatore reale è scritto nel testo di soluzione/ticket e in
un audit locale SQLite (`/api/audit`), perché le scritture su GLPI avvengono con l'account di servizio.

## Setup in GLPI (da fare prima del primo avvio)
1. Abilita la REST API (Configurazione → Generale → API) e crea un **client API** con App-Token; limita per IP all'host dell'app.
2. Crea un utente di servizio `svc-controlroom` con **profilo dedicato** (solo entità 14, non ricorsivo): ticket vedere/creare/aggiornare,
   soluzioni aggiungere, posizioni lettura. Niente diritti di amministrazione. Genera il suo **user token** (API remote access).
3. Verifica gli ID in `.env`: entità (14), origine Telefono (8), tipi soluzione (1/2/3).
4. Verifica i campi di ricerca: `1`=titolo ticket / nome posizione, `12`=stato, `15`=data apertura, `80`=entità
   (`GET /apirest.php/listSearchOptions/Ticket` e `/Location`). Vedi "Cosa non è verificato".

## Avvio
    cp .env.example .env   # compila tutto
    docker compose up -d --build
Metti davanti un reverse proxy con TLS (cookie `Secure`, `SameSite=Strict`). Rete: solo LAN Control Room.

## Sviluppo / test
    pip install -r requirements-dev.txt
    pytest                                   # GLPI e LDAP simulati in-process
    uvicorn mock.mock_glpi:app --port 9000   # GLPI finto
    GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 \
      uvicorn app.main:create_app --factory --port 8000    # login: qualunque utente, password "dev"
`DEV_AUTH=1` disattiva l'AD: **mai in produzione**. Ruoli in sviluppo: `DEV_ROLES="capo:manager,both:operator+manager"`
(gli utenti non elencati sono operatori).

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
