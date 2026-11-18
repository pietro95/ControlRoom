# Runbook — Control Room in produzione

Installazione, aggiornamento ed esercizio sul server Docker aziendale condiviso, con le stesse convenzioni di
gestione-contratti. Le parti da adattare sono indicate come `<SEGNAPOSTO>`.

## 0. Principi

- **Tutto in una cartella**: `/appdata/controlroom` (`CR_BASE`). Fuori da lì si tocca solo Nginx Proxy Manager (§5).
- **Nessun impatto sugli altri container**: progetto Compose fisso `controlroom`; lo script agisce solo sui suoi
  container e sulle sue immagini; porta non standard; nessun volume Docker nominato.
- **Si installano versioni** (tag git), mai branch, mai modifiche a mano del codice sul server.

| Cartella | Contenuto | Permessi |
|---|---|---|
| `app/` | checkout git, **mai modificato a mano** (lo script rifiuta di procedere) | utente |
| `config/` | `.env` (600), `docker-compose.override.yml` generato, `certs/` (CA interna per LDAPS) | 700 |
| `csv/` | `site_reparto.csv`, `excluded_tickets.csv` (modificabili; letti dal sync) | 755 |
| `data/` | `audit/` (uid 10001), `postgres/` (70, 700), `grafana/` (472) | |
| `backup/` | dump e archivi, anche automatici prima di ogni aggiornamento | 700 |

## 1. Setup in GLPI (prima del primo avvio)

1. Abilitare la REST API (Configurazione → Generale → API) e creare un **client API** con App-Token, limitato per IP
   all'host dell'app.
2. Creare l'utente di servizio `svc-controlroom` con **profilo dedicato** (solo entità 14, non ricorsivo): ticket
   vedere/creare/aggiornare, soluzioni aggiungere, posizioni lettura, utenti lettura. Niente diritti di
   amministrazione. Generarne il **user token** (API remote access).
3. Verificare gli ID in `.env`: entità (14), origine Telefono (8), tipi soluzione (1/2/3), stati attesa/ripresa (4/2).
4. Verificare i campi di ricerca: `1` = titolo ticket / nome posizione, `12` = stato, `15` = data apertura,
   `80` = entità (`GET /apirest.php/listSearchOptions/Ticket` e `/Location`), oltre alle `GLPI_SO_*` del sync
   (§3, `check-glpi`).

## 2. Deploy key (una volta)

Il server scarica il codice con una chiave SSH dedicata, **in sola lettura e solo su questo repository**. Sul server:

```bash
ssh-keygen -t ed25519 -N "" -C "deploy controlroom" -f ~/.ssh/controlroom_deploy && cat ~/.ssh/controlroom_deploy.pub
```

Su GitHub: ControlRoom → Settings → Deploy keys → Add deploy key, **senza** "Allow write access". Poi:

```bash
cat >> ~/.ssh/config <<'FINE'
Host github-controlroom
  HostName github.com
  User git
  IdentityFile ~/.ssh/controlroom_deploy
  IdentitiesOnly yes
FINE
```

```bash
ssh -T github-controlroom
```

Atteso: `Hi pietro95/ControlRoom! You've successfully authenticated`.

## 3. Installazione

Copiare `scripts/deploy.sh` sul server solo per il primo `prepare` (poi si usa quello in `app/`):

```bash
bash deploy.sh prepare
```

```bash
nano /appdata/controlroom/config/.env
```

```bash
/appdata/controlroom/app/scripts/deploy.sh install <VERSIONE>
```

```bash
/appdata/controlroom/app/scripts/deploy.sh check-glpi
```

```bash
/appdata/controlroom/app/scripts/deploy.sh sync
```

`prepare` crea cartelle e permessi, clona il repository, copia i CSV e genera `config/.env` con i segreti.

**Da compilare in `config/.env`**: `APP_PORT` (porta libera e non standard, lo script lo verifica), `APP_BIND` (con
Nginx Proxy Manager in container: l'IP con cui il container raggiunge l'host, di solito `172.17.0.1`), token GLPI,
`LDAP_URL` e DN dei gruppi, `PORTAL_PUBLIC_URL` (`https://<NOME_DNS>`). **Attenzione**: `LDAP_*` e
`PORTAL_PUBLIC_URL` hanno valori d'esempio, quindi non risultano "vuoti": vanno controllati a mano.

**CA interna per LDAPS**: `prepare` crea `config/certs/` e, se esiste, copia `ca-interna.pem` di gestione-contratti
(la stessa CA firma il DC); altrimenti va copiato a mano in `config/certs/ca-interna.pem`. Lo script si ferma se il
file indicato da `LDAP_CA_CERT_FILE` manca o se `LDAP_VALIDATE_TLS=false`; l'app stessa non parte se il file
configurato non esiste.

**`check-glpi`** verifica in sola lettura sul GLPI reale le search option `GLPI_SO_*`; solo se riesce abilita `sync`.

## 4. Comandi di `deploy.sh`

| Comando | Effetto |
|---|---|
| `prepare` | cartelle e permessi, clone (se manca), CSV, `config/.env` con segreti generati (se manca) |
| `install <versione>` | backup se già installato → checkout del tag → controlli → build `controlroom:<versione>` → avvio → attesa di salute e di `/healthz` → pulizia immagini vecchie del progetto; riavvia il sync se era attivo e la configurazione GLPI non è cambiata dopo `check-glpi` |
| `check-glpi` | verifica delle search option sul GLPI reale |
| `sync` | avvia il worker (solo dopo `check-glpi` riuscito) e mostra stato ed esclusioni |
| `status` | versione, servizi, ultima sincronizzazione |
| `backup [etichetta]` | `pg_dump` + archivio di audit, grafana, config (permessi 600) in `backup/` |

**Lo script si ferma su**: `.env` incompleto, `DEV_AUTH`/`DEV_ROLES` attivi, `COOKIE_SECURE` spento,
`LDAP_VALIDATE_TLS` spento, CA interna mancante, password del sync non alfanumerica, Compose < 2.24, porta occupata,
subnet `172.30.99.0/28` in uso, modifiche locali in `app/`, `sync` senza `check-glpi` riuscito o con impostazioni GLPI
cambiate dopo il check.

`install` aggiorna anche `deploy.sh` stesso mentre è in esecuzione: è sicuro perché `git checkout` crea un file
nuovo (verificato da `tests/test_deploy_selfupdate.py`). Non sovrascrivere lo script sul posto con `cp` o un editor.

## 5. Nginx Proxy Manager

Proxy Host → Domain `<NOME_DNS>`, Scheme `http`, Forward Hostname/IP = `APP_BIND`, Forward Port = `APP_PORT`,
"Block Common Exploits"; scheda SSL: certificato e "Force SSL". Grafana non va pubblicato: passa dal portale sotto
`/grafana/`. Verifica dal server:

```bash
curl -H "Host: <NOME_DNS>" http://<APP_BIND>:<APP_PORT>/healthz
```

## 6. Rilascio, aggiornamento e rollback

Rilascio: tag annotato sul commit di `main` approvato, mai spostato o riutilizzato.

```bash
git tag -a <VERSIONE> -m "<NOTE>" && git push origin <VERSIONE>
```

Aggiornamento: `deploy.sh install <VERSIONE>` (fa un backup prima). Rollback: `install <TAG_PRECEDENTE>`, più il
ripristino manuale del dump in `backup/` se la versione nuova ha modificato lo schema `reporting`.

## 7. Dopo il primo avvio

Prima di fidarsi dei numeri, eseguire le verifiche elencate in [VERIFICHE](VERIFICHE.md#cosa-non-è-verificato), in
particolare il conteggio dei ticket esclusi dalla chiusura massiva:

```sql
SELECT count(*) FROM reporting.excluded_ticket WHERE reason LIKE 'chiusura%';
```

Se il criterio prende troppo o troppo poco, usare `csv/excluded_tickets.csv` e svuotare `MASS_CLOSURE_MINUTES`.

## 8. Note per l'ambiente di sviluppo

I ruoli Postgres sono creati da `db/init/01_roles.sh` **solo alla prima inizializzazione** del volume: cambiare in
seguito `SYNC_DB_PASSWORD`/`GRAFANA_DB_PASSWORD` richiede `ALTER ROLE ... PASSWORD` a mano. In sviluppo l'audit SQLite
sta nel volume nominato `appdata` (un bind mount creato da Docker come root non era scrivibile dal container).
