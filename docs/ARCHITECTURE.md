# Architettura di Control Room

## Contesto

```text
browser operatore/manager ──HTTPS── Nginx Proxy Manager ──HTTP── app (portale)
                                                                  ├── GLPI REST API (account di servizio)
                                                                  ├── Active Directory (LDAPS, bind utente)
                                                                  └── /grafana/* ── grafana ── postgres (viste v_*)
                                         sync (worker) ── GLPI ──► postgres (schema reporting)
```

## Servizi del compose

| Servizio | Reti | Porte pubblicate | Note |
|---|---|---|---|
| `app` | default, `grafana_net` (IP fisso 172.30.99.10) | `APP_BIND:APP_PORT` | portale; nessuna dipendenza da Postgres/Grafana |
| `grafana` | `grafana_net` (172.30.99.11), `db_net` | nessuna | auth proxy accettato solo dall'IP dell'app; reti interne, niente Internet |
| `postgres` | `db_net` | nessuna | schema `reporting`; ruoli `cr_sync` (scrive) e `grafana_ro` (solo SELECT, sessioni read-only) |
| `sync` | default (GLPI), `db_net` | nessuna | stessa immagine dell'app, `python -m sync.worker`; audit dell'app montato in sola lettura |

`app` e `sync` usano l'immagine `controlroom:<versione>`; Postgres `postgres:16-alpine`; Grafana
`grafana/grafana-oss:11.4.0`.

## Regole di dipendenza

- Il portale non dipende dal reporting: con Postgres o Grafana fermi, coda allarmi e nuova chiamata funzionano.
- Grafana legge solo le viste `v_*` come `grafana_ro`; le regole KPI stanno solo in `sync/schema.sql`
  (vedi [REPORTING](REPORTING.md)).
- Il sync scrive su GLPI una sola cosa: la Posizione degli alert aperti che ne sono privi.

## Flussi principali

### Coda e azioni dell'operatore

Il portale legge la coda da GLPI con cache condivisa (`QUEUE_CACHE_SECONDS`), esegue in parallelo le chiamate GLPI
(`GLPI_CONCURRENCY`) e, a ogni scrittura, invalida la cache e registra l'operatore nell'audit SQLite.

### Statistiche

Il portale verifica il ruolo manager e inoltra `/grafana/*` a Grafana con `X-WEBAUTH-USER=<utente AD>`
(utente Grafana creato al primo accesso, ruolo **Viewer**). Misure del proxy in [SECURITY](SECURITY.md#grafana).

### Sync

Incrementale per data di modifica con sovrapposizione, upsert transazionale, riconciliazione periodica delle
cancellazioni (dettagli in [REPORTING §1](REPORTING.md#1-sync-glpi--postgresql)).

## Deployment

Server Docker condiviso, progetto Compose fisso `controlroom`, nessun volume Docker nominato in produzione: dati in
cartelle sotto `/appdata/controlroom`. Procedura in [RUNBOOK](RUNBOOK.md).
