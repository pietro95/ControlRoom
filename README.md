<p align="center">
  <img src="app/static/img/logo/siciliacque.svg" alt="Siciliacque S.p.A." width="460">
</p>

<h1 align="center">Control Room</h1>

<p align="center">
  Portale operatore e statistiche della Control Room Centralizzata, costruito sopra GLPI 11.
</p>

> [!IMPORTANT]
> Il repository non contiene dati aziendali reali. Il GLPI simulato (`mock/`) e il dataset di `scripts/gen_synthetic.py`
> sono inventati: i numeri che producono non dicono nulla sulla realtà.

## Panoramica

Control Room è un'interfaccia a pulsanti sopra la REST API legacy di GLPI 11, limitata all'entità Control Room.
Non sostituisce GLPI: ogni dato vive nei ticket GLPI e, se il portale cade, resta il modulo GLPI nativo.

| Ruolo (gruppo AD) | Funzioni |
|---|---|
| **operatore** | coda **Allarmi** (raggruppamento, presa in carico, Rientrato / Manovra, attesa e ripresa) e **Nuova chiamata** dal campo; unico ruolo che scrive su GLPI |
| **manager** | **Statistiche**: dashboard Grafana in sola lettura, alimentate da un worker di sync GLPI → PostgreSQL |

Comportamento dettagliato: [SPEC](docs/SPEC.md). Regole KPI e dashboard: [REPORTING](docs/REPORTING.md).

## Stato del progetto

Fasi 1–4 completate (portale, reporting, dashboard). **Mai provato contro il GLPI e l'Active Directory reali**:
flussi e parametri sono testati su GLPI e LDAP simulati. L'elenco puntuale di cosa è verificato, cosa no e i limiti
noti è in [VERIFICHE](docs/VERIFICHE.md): va letto prima del primo avvio in produzione.

## Architettura

| Componente | Responsabilità |
|---|---|
| `app/` | portale FastAPI: login AD, coda allarmi, nuova chiamata, audit SQLite, proxy verso Grafana |
| `app/static/` | pagine HTML/JS/CSS, tema Siciliacque, CSS iniettato in Grafana |
| `sync/` | worker GLPI → PostgreSQL, regole KPI (`rules.py`, viste in `schema.sql`), `check_glpi` |
| `db/init/` | ruoli PostgreSQL `cr_sync` (scrittura) e `grafana_ro` (sola lettura) |
| `grafana/` | provisioning di datasource e dashboard (JSON generati) |
| `scripts/` | `deploy.sh`, generatore delle dashboard, dataset sintetico |
| `config/` | CSV modificabili: mapping sito → reparto, ticket esclusi |
| `mock/` | GLPI e Grafana simulati per sviluppo e test |
| `tests/` | test pytest (KPI e worker su PostgreSQL 16 reale) |

Stack: Python 3.12, FastAPI/Uvicorn, PostgreSQL 16, Grafana OSS 11.4.0, Docker Compose.
Dettagli su servizi, reti e flussi: [ARCHITECTURE](docs/ARCHITECTURE.md).

## Avvio in sviluppo

```bash
pip install -r requirements-dev.txt
```

```bash
uvicorn mock.mock_glpi:app --port 9000
```

```bash
GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 \
  uvicorn app.main:create_app --factory --port 8000
```

Login: qualunque utente, password `dev`. Ruoli: `DEV_ROLES="capo:manager,both:operator+manager"` (gli utenti non
elencati sono operatori). `DEV_AUTH=1` disattiva l'AD: **mai in produzione**.

Stack completo con Docker (Postgres, Grafana, sync):

```bash
cp .env.example .env    # compilare tutto; password: openssl rand -hex 24
```

```bash
docker compose up -d --build
```

## Qualità e test

```bash
pytest
```

GLPI e LDAP sono simulati in-process; i test di KPI, worker e dashboard creano un cluster PostgreSQL temporaneo
(serve PostgreSQL installato, oppure `PG_BIN`). Le dashboard si modificano solo nel builder:

```bash
python -m scripts.build_dashboards
```

Un test fallisce se `grafana/dashboards/*.json` non è allineato al builder.

## Produzione

Server Docker aziendale condiviso, tutto sotto `/appdata/controlroom`, dietro Nginx Proxy Manager, stesse convenzioni
di gestione-contratti. Si installano solo versioni (tag git) con `scripts/deploy.sh`.

- [RUNBOOK](docs/RUNBOOK.md): setup GLPI, deploy key, installazione, aggiornamento, rollback, reverse proxy;
- [variabili d'ambiente di esempio](.env.example).

## Documentazione

Indice e autorità dei documenti: [docs/README.md](docs/README.md).

| Documento | Contenuto |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | comportamento del portale, ruoli, regole operative |
| [`docs/REPORTING.md`](docs/REPORTING.md) | sync GLPI → PostgreSQL, regole KPI R1–R7, mapping reparti, dashboard |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | servizi, reti, proxy Grafana, flussi |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | installazione ed esercizio in produzione |
| [`docs/SECURITY.md`](docs/SECURITY.md) | autenticazione, segreti, isolamento, dati |
| [`docs/VERIFICHE.md`](docs/VERIFICHE.md) | cosa è verificato, cosa no, limiti noti |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | regole per contribuire |

## Sicurezza e dati

- Non committare `.env`, token GLPI, certificati o password.
- Non inserire nelle fixture dati reali (ticket, nomi di siti, operatori).
- Trattare dump, backup e audit come dati aziendali riservati.

Il logo e l'identità visiva sono riservati all'applicazione interna: vedere
[`app/static/img/logo/README.md`](app/static/img/logo/README.md).
