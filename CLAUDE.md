# CLAUDE.md — Control Room (Siciliacque)

Portale operatore sopra GLPI 11 (coda allarmi, nuova chiamata) e statistiche per i manager (sync GLPI → PostgreSQL,
dashboard Grafana). GLPI resta il sistema di registrazione: il portale non deve diventare indispensabile.

## Documenti da leggere prima di lavorare

- `docs/SPEC.md`: comportamento del portale e ruoli. È il riferimento per il portale.
- `docs/REPORTING.md`: sync, regole KPI R1–R7, mapping reparti, dashboard. È il riferimento per i numeri.
- `docs/ARCHITECTURE.md`: servizi, reti, regole di dipendenza.
- `docs/SECURITY.md`: autenticazione, proxy Grafana, privilegi DB.
- `docs/RUNBOOK.md`: produzione, `scripts/deploy.sh`, utente di servizio e comando `controlroom`.
- `docs/VERIFICHE.md`: cosa è provato e cosa no. **Nulla è mai stato provato contro GLPI e AD reali.**

Se SPEC o REPORTING sono ambigui o in conflitto con la richiesta, fermati e chiedi. Non scegliere in silenzio.

## Stack approvato (non cambiarlo senza una decisione esplicita)

- Python 3.12 (non 3.13+: rifiuta CA senza *keyUsage*, vedi VERIFICHE), FastAPI + Uvicorn, `httpx`, `ldap3`,
  `itsdangerous`, `psycopg` 3.
- Frontend: HTML/JS/CSS statici in `app/static/`, niente framework SPA, niente CDN (font in `app/static/vendor/`).
- Audit locale SQLite; reporting su PostgreSQL 16; Grafana OSS 11.4.0 con provisioning.
- Test: `pytest`, GLPI/LDAP/Grafana simulati (`mock/`), PostgreSQL reale temporaneo per KPI e worker.
- Produzione: Docker Compose, progetto `controlroom`, `/appdata/controlroom`, dietro Nginx Proxy Manager; codice e
  configurazione dell'utente di servizio `controlroom`, esercizio con il comando `controlroom` (mai lavoro come root).

## Comandi

```bash
pip install -r requirements-dev.txt
pytest                                       # tutti i test; quelli di reporting saltano senza PostgreSQL (PG_BIN)
python -m scripts.build_dashboards           # rigenera grafana/dashboards/*.json dal builder
uvicorn mock.mock_glpi:app --port 9000       # GLPI simulato
GLPI_URL=http://localhost:9000 GLPI_APP_TOKEN=a GLPI_USER_TOKEN=u DEV_AUTH=1 \
  uvicorn app.main:create_app --factory --port 8000   # login: qualunque utente, password "dev"
docker compose up -d --build                 # stack completo (serve .env)
bash -n scripts/deploy.sh                    # sintassi degli script di deploy (anche controlroom.sh, installa_utente_servizio.sh)
```

## Struttura

```
app/          portale FastAPI: main.py (route), glpi.py (client), auth.py (LDAP/ruoli), parsing.py (corpo ticket),
              store.py (audit SQLite), grafana_proxy.py, static/
sync/         worker.py, rules.py, schema.sql (viste v_* = regole KPI), check_glpi.py
db/init/      ruoli PostgreSQL cr_sync e grafana_ro
grafana/      provisioning; dashboards/*.json GENERATI
scripts/      deploy.sh, controlroom.sh (comando di esercizio), installa_utente_servizio.sh, build_dashboards.py,
              gen_synthetic.py
config/       CSV modificabili (site_reparto, excluded_tickets)
mock/         GLPI e Grafana simulati
tests/
```

## Regole non negoziabili

1. **Nessun dato reale nel repository**: niente ticket, siti, operatori o token reali in test, mock, screenshot.
2. **Nessun segreto nel codice o nei commit.** Ogni variabile nuova va in `.env.example`; se tocca la sicurezza,
   anche nei controlli di `deploy.sh`.
3. **Regole KPI solo in `sync/schema.sql`** (viste `v_*`); Grafana legge solo le viste. Ogni regola ha un test in
   `tests/test_kpi.py` che fallisce se la regola è rotta. Tempi da `solvedate`, mai `closedate`; ore in Europe/Rome.
4. **Dashboard solo dal builder** (`scripts/build_dashboards.py`): mai modificare i JSON a mano.
5. **Il portale non dipende dal reporting**: con Postgres o Grafana fermi, coda e nuova chiamata funzionano.
6. **Scritture su GLPI solo dal ruolo operatore**, sempre tracciate con l'operatore reale (testo + audit). Ogni
   endpoint di scrittura ha un test del 403 per il manager e passa da `write_guard`.
7. **Proxy Grafana**: non inoltrare mai al browser né da esso `X-WEBAUTH-*`, `X-Grafana-*`, `Authorization`, cookie
   del portale. Modifiche al proxy richiedono `tests/test_grafana_proxy.py` verde.
8. **AD e GLPI reali mai contattati nei test.** `DEV_AUTH`/`DEV_ROLES` solo in sviluppo.
9. **Verifica TLS del DC sempre attiva** in produzione; niente scorciatoie.

## Convenzioni

- Interfaccia e documentazione in italiano; date `31/12/2026`; fuso `Europe/Rome`.
- Commit piccoli e descrittivi in italiano; PR con il template di `.github/`.
- Aggiornare nello stesso commit la documentazione toccata, e `docs/VERIFICHE.md` se cambia cosa è provato.
- Le versioni si rilasciano con tag annotati, mai spostati.

## Cosa NON fare

- Non aggiungere dipendenze fuori stack senza motivarlo nella PR.
- Non modificare regole KPI o comportamento della SPEC di tua iniziativa.
- Non disattivare o saltare test per far passare la CI.
- Non introdurre chiamate a servizi esterni (CDN, telemetria, API pubbliche).
- Non dichiarare "verificato" ciò che è stato provato solo sul mock.
