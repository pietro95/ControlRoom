-- Schema di reporting (proprietario cr_sync). Applicato dal worker di sync a ogni avvio: idempotente.
-- Grafana (grafana_ro) legge SOLO le viste v_* (SELECT concesso dai default privileges di roles.psql).
--
-- REGOLE KPI (fonte di verità, ognuna coperta da tests/test_kpi.py):
--  R1 statistiche alert = solo ticket "ALERT ON" (kind='alert_on'); ALERT OFF, "Prova ...", altri titoli esclusi.
--  R2 esclusi i ticket in excluded_ticket (chiusura massiva 20/08/2026 17:42 ora di Roma + elenco manuale) e i
--     ticket cancellati in GLPI (deleted=true dopo la riconciliazione).
--  R3 tempi di gestione = solved_at - opened_at (solvedate, opzione 17). MAI closedate. Senza solvedate: escluso
--     dalle statistiche dei tempi (ttr_min NULL).
--  R4 ripetizione = alert sullo stesso codice sito aperto entro N minuti (inclusi) dal precedente alert valido dello
--     stesso sito; N = setting 'flapping_window_min' (default 60).
--  R5 fasce orarie e giorno della settimana sull'ora di APERTURA convertita in Europe/Rome (gestisce l'ora legale).
--  R6 esito = tipo soluzione (opzione 23): Rientrato / Manovra / Altro; vuoto = 'Non classificato' (storico).
--  R7 chi ha gestito = audit dell'app o riga "[Control Room app — operatore: ...]"; altrimenti 'sconosciuto'.
--     Il campo GLPI "ultima modifica di" NON è usato.

CREATE TABLE IF NOT EXISTS ticket (
    id            integer PRIMARY KEY,
    title         text        NOT NULL,
    kind          text        NOT NULL CHECK (kind IN ('alert_on', 'alert_off', 'prova', 'call', 'other')),
    site_code     text,
    acquedotto    text,
    status        smallint,
    opened_at     timestamptz,
    solved_at     timestamptz,
    closed_at     timestamptz,
    modified_at   timestamptz,
    solution_type text,
    request_type  text,
    location      text,
    reparto_glpi  text,
    solution_operator_name text,      -- dalla riga dell'app nel testo della soluzione
    solution_operator_user text,
    deleted       boolean     NOT NULL DEFAULT false,
    synced_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_site_opened ON ticket (site_code, opened_at);
CREATE INDEX IF NOT EXISTS ticket_kind_opened ON ticket (kind, opened_at);

-- operatore dall'audit locale dell'app (SQLite, letto in sola lettura dal worker)
CREATE TABLE IF NOT EXISTS handler (
    ticket_id    integer PRIMARY KEY,
    username     text NOT NULL,
    display_name text,
    action       text NOT NULL,
    ts           timestamptz
);

CREATE TABLE IF NOT EXISTS excluded_ticket (
    ticket_id integer PRIMARY KEY,
    reason    text NOT NULL,
    added_at  timestamptz NOT NULL DEFAULT now()
);

-- mapping manuale codice sito -> reparto (caricato da CSV); prevale su quello dedotto dalle Posizioni GLPI
CREATE TABLE IF NOT EXISTS site_reparto_manual (
    site_code text PRIMARY KEY,
    reparto   text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS setting (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO setting VALUES ('flapping_window_min', '60') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS sync_state (
    key        text PRIMARY KEY,
    value      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- viste (ricreate a ogni avvio)
DROP VIEW IF EXISTS v_sync_status, v_mapping_coverage, v_backlog, v_call, v_alert, v_site_reparto, v_ticket_valid CASCADE;

-- R2: ticket validi per le statistiche (non cancellati, non esclusi)
CREATE VIEW v_ticket_valid AS
SELECT t.*
FROM ticket t
WHERE NOT t.deleted
  AND NOT EXISTS (SELECT 1 FROM excluded_ticket e WHERE e.ticket_id = t.id);

-- reparto per sito: mapping manuale, altrimenti il reparto più frequente nelle Posizioni GLPI di quel sito
CREATE VIEW v_site_reparto AS
WITH glpi AS (
    SELECT site_code, reparto_glpi AS reparto,
           row_number() OVER (PARTITION BY site_code ORDER BY count(*) DESC, reparto_glpi) AS rn
    FROM ticket
    WHERE site_code IS NOT NULL AND reparto_glpi IS NOT NULL
    GROUP BY site_code, reparto_glpi
)
SELECT coalesce(m.site_code, g.site_code) AS site_code,
       coalesce(m.reparto, g.reparto)     AS reparto,
       CASE WHEN m.site_code IS NOT NULL THEN 'manuale' ELSE 'posizione GLPI' END AS fonte
FROM site_reparto_manual m
FULL JOIN (SELECT site_code, reparto FROM glpi WHERE rn = 1) g ON g.site_code = m.site_code;

-- R1..R7: un alert per riga
CREATE VIEW v_alert AS
WITH w AS (SELECT make_interval(mins => value::int) AS win FROM setting WHERE key = 'flapping_window_min'),
a AS (
    SELECT t.*,
           lag(t.opened_at) OVER (PARTITION BY coalesce(t.site_code, '#' || t.id)
                                  ORDER BY t.opened_at, t.id) AS prev_opened_at
    FROM v_ticket_valid t
    WHERE t.kind = 'alert_on'
)
SELECT a.id, a.title, a.site_code, a.acquedotto,
       coalesce(r.reparto, 'non mappato')                       AS reparto,
       a.status, a.status < 5                                    AS is_open,
       a.opened_at, a.solved_at, a.closed_at,
       (a.opened_at AT TIME ZONE 'Europe/Rome')                  AS opened_local,
       extract(hour   FROM a.opened_at AT TIME ZONE 'Europe/Rome')::int AS hour_local,
       extract(isodow FROM a.opened_at AT TIME ZONE 'Europe/Rome')::int AS dow_local,   -- 1 = lunedì
       (a.opened_at AT TIME ZONE 'Europe/Rome')::date            AS day_local,
       a.prev_opened_at,
       (a.site_code IS NOT NULL AND a.prev_opened_at IS NOT NULL
        AND a.opened_at - a.prev_opened_at <= (SELECT win FROM w)) AS is_repeat,
       CASE WHEN a.solved_at IS NOT NULL AND a.solved_at >= a.opened_at
            THEN extract(epoch FROM a.solved_at - a.opened_at) / 60.0 END AS ttr_min,
       CASE WHEN a.solution_type ILIKE '%rientrat%' THEN 'Rientrato'
            WHEN a.solution_type ILIKE '%manovra%'  THEN 'Manovra'
            WHEN nullif(btrim(a.solution_type), '') IS NOT NULL THEN 'Altro'
            ELSE 'Non classificato' END                          AS outcome,
       coalesce(h.display_name, a.solution_operator_name, 'sconosciuto') AS handled_by
FROM a
LEFT JOIN v_site_reparto r ON r.site_code = a.site_code
LEFT JOIN handler h ON h.ticket_id = a.id;

-- chiamate dal campo: titolo "CHIAMATA:" (creato dall'app con origine Telefono)
CREATE VIEW v_call AS
SELECT t.id, t.title, t.site_code, t.acquedotto, t.request_type, t.status, t.status < 5 AS is_open,
       t.opened_at, t.solved_at,
       (t.opened_at AT TIME ZONE 'Europe/Rome')::date AS day_local,
       extract(hour FROM t.opened_at AT TIME ZONE 'Europe/Rome')::int AS hour_local,
       CASE WHEN t.solved_at IS NOT NULL AND t.solved_at >= t.opened_at
            THEN extract(epoch FROM t.solved_at - t.opened_at) / 60.0 END AS ttr_min,
       coalesce(h.display_name, t.solution_operator_name, 'sconosciuto') AS handled_by
FROM v_ticket_valid t
LEFT JOIN handler h ON h.ticket_id = t.id
WHERE t.kind = 'call';

-- backlog: alert aperti ora (stato GLPI < 5) con età
CREATE VIEW v_backlog AS
SELECT id, site_code, acquedotto, reparto, opened_at,
       extract(epoch FROM now() - opened_at) / 3600.0 AS age_hours
FROM v_alert
WHERE is_open;

-- copertura del mapping sito -> reparto
CREATE VIEW v_mapping_coverage AS
SELECT count(DISTINCT site_code)                                            AS siti_totali,
       count(DISTINCT site_code) FILTER (WHERE reparto <> 'non mappato')    AS siti_mappati,
       count(*)                                                             AS alert_totali,
       count(*) FILTER (WHERE reparto <> 'non mappato')                     AS alert_mappati
FROM v_alert
WHERE site_code IS NOT NULL;

-- freschezza dei dati: Grafana mostra sempre l'ultima sincronizzazione RIUSCITA
CREATE VIEW v_sync_status AS
SELECT max(CASE WHEN key = 'last_success_at' THEN value END)::timestamptz AS last_success_at,
       max(CASE WHEN key = 'last_attempt_at' THEN value END)::timestamptz AS last_attempt_at,
       max(CASE WHEN key = 'last_error'      THEN value END)              AS last_error,
       max(CASE WHEN key = 'last_reconcile_at' THEN value END)::timestamptz AS last_reconcile_at,
       -- valorizzato SOLO dal generatore di dati sintetici: le dashboard lo mostrano in evidenza
       coalesce(max(CASE WHEN key = 'dataset_label' THEN value END), 'GLPI')  AS dataset_label,
       (SELECT count(*) FROM ticket)                                      AS ticket_in_db
FROM sync_state;
