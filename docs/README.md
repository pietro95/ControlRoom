# Documentazione

## Orientamento

| Esigenza | Documento |
|---|---|
| Comprendere scopo e avvio rapido | [README principale](../README.md) |
| Sapere cosa fa il portale per operatori e manager | [SPEC](SPEC.md) |
| Capire come si calcolano i numeri delle dashboard | [REPORTING](REPORTING.md) |
| Comprendere servizi, reti e flussi | [ARCHITECTURE](ARCHITECTURE.md) |
| Installare o aggiornare la produzione | [RUNBOOK](RUNBOOK.md) |
| Valutare sicurezza e trattamento dati | [SECURITY](SECURITY.md) |
| Sapere cosa è provato e cosa no | [VERIFICHE](VERIFICHE.md) |
| Contribuire al codice o ai documenti | [CONTRIBUTING](../CONTRIBUTING.md) |
| Identità visiva | [design/](design/), [logo](../app/static/img/logo/README.md) |

## Autorità dei documenti

In caso di incoerenza:

1. SPEC e REPORTING definiscono il comportamento richiesto (REPORTING è autorevole per le regole KPI R1–R7);
2. i test (`tests/test_kpi.py` per le regole KPI) sono gli esempi verificabili;
3. il codice e `sync/schema.sql` descrivono lo stato implementato;
4. ARCHITECTURE spiega la struttura senza ridefinire le regole;
5. RUNBOOK descrive l'esercizio;
6. VERIFICHE descrive lo stato delle prove, non il comportamento.

Un conflitto va risolto esplicitamente nella pull request: non si adattano test o documentazione al codice in silenzio.

## Regole editoriali

- Usare ruoli e responsabilità, non nomi di persone.
- Non duplicare procedure: rimandare al documento autorevole.
- Aggiornare i link nello stesso commit che sposta o rinomina un file.
- Non inserire dati reali, segreti o dettagli operativi sensibili.
- Distinguere ciò che è verificato da ciò che non lo è; aggiornare VERIFICHE quando cambia.
- Scrivere in italiano.
