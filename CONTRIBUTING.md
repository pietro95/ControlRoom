# Contribuire a Control Room

## Prima di iniziare

Leggere [README](README.md), [SPEC](docs/SPEC.md) e, se si toccano sync o dashboard, [REPORTING](docs/REPORTING.md).

## Branch e pull request

- Nessun commit diretto su `main`: un branch per modifica, pull request con il [template](.github/pull_request_template.md).
- Le versioni si rilasciano solo con tag annotati (vedi [RUNBOOK §6](docs/RUNBOOK.md#6-rilascio-aggiornamento-e-rollback)).

## Regole di sviluppo

- Le regole KPI stanno solo nelle viste di `sync/schema.sql`; ogni regola nuova o modificata ha un test in
  `tests/test_kpi.py` che fallisce se la regola è rotta.
- Le dashboard si modificano in `scripts/build_dashboards.py` e si rigenerano; mai a mano i JSON.
- Il portale non deve dipendere da Postgres o Grafana.
- Ogni nuova variabile d'ambiente va in `.env.example` e, se riguarda la sicurezza, nei controlli di `deploy.sh`.

## Dati e segreti

- Nessun dato reale (ticket, siti, operatori) in test, fixture, screenshot o documenti.
- Nessun segreto nel repository.

## Verifica locale

```bash
pytest
```

```bash
python -m scripts.build_dashboards && git diff --exit-code grafana/dashboards
```

## Modifiche alla documentazione

Aggiornare nello stesso commit i documenti toccati dalla modifica (vedi [indice](docs/README.md)); se cambia ciò che
è provato sul campo, aggiornare [VERIFICHE](docs/VERIFICHE.md).
