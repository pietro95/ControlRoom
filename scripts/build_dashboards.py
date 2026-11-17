"""Genera le dashboard Grafana (grafana/dashboards/*.json) dalle definizioni qui sotto.

    python -m scripts.build_dashboards

I JSON generati sono versionati e provisionati in sola lettura (grafana/provisioning/dashboards/controlroom.yaml).
Per cambiare una dashboard: modificare questo file e rigenerare, non i JSON a mano.
Tutte le query leggono SOLO le viste v_* (regole KPI in sync/schema.sql) e sono eseguite dal test
tests/test_dashboards.py come grafana_ro su PostgreSQL reale.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "reporting"}
ACQ = "coalesce(acquedotto, '—') IN ($acquedotto)"
REP = "reparto IN ($reparto)"
TF = "$__timeFilter(opened_at)"
# Palette Siciliacque (come app/static/style.css), in tinte leggibili sia su tema chiaro sia su scuro:
# i colori fissi delle dashboard non cambiano col tema.
BLU = "#2b8fd6"     # serie principali (tra blu medio #005A94 e azzurro #5CBBFF)
OK = "#2a9d5c"      # esito positivo / rientrato
WARN = "#d08a1f"    # manovra, attenzione
BAD = "#d64545"     # errore
GRIGIO = "#8a9bb0"  # non classificato


def target(sql: str, fmt: str = "table", ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "rawSql": " ".join(sql.split()), "format": fmt,
            "rawQuery": True, "editorMode": "code"}


def panel(kind: str, title: str, sql: str | list, x: int, y: int, w: int, h: int, fmt: str = "table",
          desc: str = "", unit: str | None = None, options: dict | None = None, field: dict | None = None,
          overrides: list | None = None) -> dict:
    sqls = sql if isinstance(sql, list) else [sql]
    defaults = {**({"unit": unit} if unit else {}), **(field or {})}
    options = dict(options or {})
    if kind == "stat":  # niente colori-soglia di default (rosso sopra 80) su conteggi e tempi
        options.setdefault("colorMode", "none")
    if kind in ("timeseries", "barchart"):
        defaults.setdefault("min", 0)
        defaults.setdefault("color", {"mode": "fixed", "fixedColor": BLU})
    return {"type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target(s, fmt, chr(65 + i)) for i, s in enumerate(sqls)],
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options}


def text(title: str, md: str, x: int, y: int, w: int, h: int) -> dict:
    return {"type": "text", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": md}}


def freshness(y: int = 0) -> list[dict]:
    """Riga comune: fonte dei dati e ultima sincronizzazione riuscita (i dati restano visibili se GLPI è giù)."""
    return [
        panel("stat", "Fonte dati", "SELECT dataset_label AS \"Fonte\" FROM v_sync_status", 0, y, 10, 3,
              desc="'GLPI' in produzione. Se compare SINTETICO i numeri sono generati da scripts/gen_synthetic.py.",
              options={"textMode": "value", "colorMode": "background", "graphMode": "none",
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"}},
              field={"color": {"mode": "fixed", "fixedColor": WARN}},
              overrides=[]),
        panel("stat", "Ultima sincronizzazione riuscita",
              # stat non visualizza campi timestamp: epoch in millisecondi + unità "tempo fa"
              "SELECT extract(epoch FROM last_success_at) * 1000 AS \"Ultima sync\" FROM v_sync_status",
              10, y, 7, 3, unit="dateTimeFromNow",
              desc="I grafici mostrano i dati fino a questo momento. Se GLPI non risponde il valore smette di avanzare.",
              options={"colorMode": "none", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}}),
        panel("stat", "Errore ultimo tentativo",
              "SELECT coalesce(last_error, 'nessuno') AS \"Errore\" FROM v_sync_status", 17, y, 7, 3,
              options={"textMode": "value", "colorMode": "none", "graphMode": "none",
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/"}}),
    ]


def var_query(name: str, label: str, sql: str) -> dict:
    return {"name": name, "label": label, "type": "query", "datasource": DS, "query": sql,
            "definition": sql, "refresh": 2, "multi": True, "includeAll": True,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}


VAR_ACQ = var_query("acquedotto", "Acquedotto",
                    "SELECT DISTINCT coalesce(acquedotto, '—') FROM v_alert ORDER BY 1")
VAR_REP = var_query("reparto", "Reparto", "SELECT DISTINCT reparto FROM v_alert ORDER BY 1")


def dashboard(uid: str, title: str, panels: list, variables: list | None = None, frm: str = "now-180d") -> dict:
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {"uid": uid, "title": title, "tags": ["controlroom"], "timezone": "Europe/Rome", "editable": False,
            "schemaVersion": 39, "version": 1, "refresh": "5m", "time": {"from": frm, "to": "now"},
            "templating": {"list": variables or []}, "panels": panels,
            "links": [{"title": "Control Room", "type": "dashboards", "tags": ["controlroom"], "asDropdown": False}]}


BAR = {"orientation": "auto", "showValue": "auto", "legend": {"showLegend": True, "displayMode": "list",
                                                             "placement": "bottom"}, "xTickLabelRotation": -45}


# ------------------------------------------------------------------ Panoramica
def overview() -> dict:
    p = freshness()
    p += [
        panel("stat", "Alert nel periodo", f"SELECT count(*) AS \"Alert\" FROM v_alert WHERE {TF} AND {ACQ}",
              0, 3, 6, 4, options={"graphMode": "none"}),
        panel("stat", "Quota ripetuti (≤ finestra)",
              f"SELECT avg(is_repeat::int) AS \"Ripetuti\" FROM v_alert WHERE {TF} AND {ACQ}", 6, 3, 6, 4,
              unit="percentunit", desc="Alert sullo stesso sito entro la finestra configurata (default 60 min) dal precedente.",
              options={"graphMode": "none"}),
        panel("stat", "Ticket aperti ora", f"SELECT count(*) AS \"Aperti\" FROM v_backlog WHERE {ACQ}", 12, 3, 6, 4,
              desc="Alert con stato GLPI non risolto, indipendentemente dal periodo selezionato.",
              options={"graphMode": "none"}),
        panel("stat", "Età mediana backlog",
              f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY age_hours) AS \"Età\" FROM v_backlog WHERE {ACQ}",
              18, 3, 6, 4, unit="h", options={"graphMode": "none"}),
        panel("timeseries", "Alert al giorno",
              f"""SELECT (day_local::timestamp AT TIME ZONE 'Europe/Rome') AS time, count(*) AS "Alert",
                         count(*) FILTER (WHERE is_repeat) AS "di cui ripetuti"
                  FROM v_alert WHERE {TF} AND {ACQ} GROUP BY 1 ORDER BY 1""", 0, 7, 24, 8, fmt="time_series",
              field={"custom": {"drawStyle": "bars", "fillOpacity": 60, "barAlignment": 0}}),
        panel("table", "Alert per ora del giorno × giorno della settimana (ora di Roma)",
              f"""SELECT lpad(h.hour_local::text, 2, '0') || ':00' AS "Ora",
                         count(*) FILTER (WHERE dow_local = 1) AS "Lun", count(*) FILTER (WHERE dow_local = 2) AS "Mar",
                         count(*) FILTER (WHERE dow_local = 3) AS "Mer", count(*) FILTER (WHERE dow_local = 4) AS "Gio",
                         count(*) FILTER (WHERE dow_local = 5) AS "Ven", count(*) FILTER (WHERE dow_local = 6) AS "Sab",
                         count(*) FILTER (WHERE dow_local = 7) AS "Dom"
                  FROM generate_series(0, 23) h(hour_local)
                  LEFT JOIN v_alert a ON a.hour_local = h.hour_local AND {TF} AND {ACQ}
                  GROUP BY h.hour_local ORDER BY h.hour_local""", 0, 15, 16, 22,
              desc="Heatmap: ogni cella è il numero di alert aperti in quell'ora e giorno nel periodo selezionato.",
              field={"custom": {"cellOptions": {"type": "color-background", "mode": "basic"}, "align": "center"},
                     "color": {"mode": "continuous-GrYlRd"}},
              overrides=[{"matcher": {"id": "byName", "options": "Ora"},
                          "properties": [{"id": "custom.cellOptions", "value": {"type": "auto"}}]}],
              options={"showHeader": True, "cellHeight": "sm"}),
        panel("barchart", "Età del backlog (ticket aperti ora)",
              f"""SELECT b.fascia AS "Età", count(v.id) AS "Ticket"
                  FROM (VALUES (1, '< 1 h'), (2, '1-4 h'), (3, '4-24 h'), (4, '1-7 gg'), (5, '> 7 gg')) b(ord, fascia)
                  LEFT JOIN (SELECT id, CASE WHEN age_hours < 1 THEN 1 WHEN age_hours < 4 THEN 2
                                             WHEN age_hours < 24 THEN 3 WHEN age_hours < 168 THEN 4 ELSE 5 END AS ord
                             FROM v_backlog WHERE {ACQ}) v USING (ord)
                  GROUP BY b.ord, b.fascia ORDER BY b.ord""", 16, 15, 8, 22, options=BAR),
    ]
    return dashboard("cr-overview", "Control Room – Panoramica", p, [VAR_ACQ], "now-30d")


# ------------------------------------------------------------------ Siti, acquedotti, reparti
def sites() -> dict:
    p = freshness()
    p += [
        panel("barchart", "Pareto: top-20 siti per numero di alert",
              f"""SELECT site_code AS "Sito", count(*) AS "Alert" FROM v_alert
                  WHERE {TF} AND {ACQ} AND {REP} AND site_code IS NOT NULL
                  GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20""", 0, 3, 14, 10, options=BAR),
        panel("table", "Pareto top-20: quota e quota cumulata sul totale",
              f"""WITH s AS (SELECT site_code, count(*) AS n, count(*) FILTER (WHERE is_repeat) AS r FROM v_alert
                             WHERE {TF} AND {ACQ} AND {REP} AND site_code IS NOT NULL GROUP BY 1),
                       t AS (SELECT site_code, n, r, n::numeric / sum(n) OVER () AS quota,
                                    sum(n) OVER (ORDER BY n DESC, site_code) / sum(n) OVER () AS cumulata
                             FROM s)
                  SELECT site_code AS "Sito", n AS "Alert", quota AS "Quota", cumulata AS "Cumulata",
                         r::numeric / n AS "Ripetuti" FROM t ORDER BY n DESC, site_code LIMIT 20""", 14, 3, 10, 10,
              overrides=[{"matcher": {"id": "byRegexp", "options": "Quota|Cumulata|Ripetuti"},
                          "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}]}]),
        panel("timeseries", "Trend settimanale per acquedotto",
              f"""SELECT (date_trunc('week', opened_local) AT TIME ZONE 'Europe/Rome') AS time,
                         coalesce(acquedotto, '—') AS metric, count(*) AS value
                  FROM v_alert WHERE {TF} AND {ACQ} AND {REP} GROUP BY 1, 2 ORDER BY 1""", 0, 13, 24, 9,
              fmt="time_series"),
        panel("table", "Quota di ripetuti per sito (siti con almeno 10 alert)",
              f"""SELECT site_code AS "Sito", acquedotto AS "Acq.", reparto AS "Reparto", count(*) AS "Alert",
                         avg(is_repeat::int) AS "Ripetuti"
                  FROM v_alert WHERE {TF} AND {ACQ} AND {REP} AND site_code IS NOT NULL
                  GROUP BY 1, 2, 3 HAVING count(*) >= 10 ORDER BY 5 DESC, 4 DESC""", 0, 22, 12, 12,
              overrides=[{"matcher": {"id": "byName", "options": "Ripetuti"},
                          "properties": [{"id": "unit", "value": "percentunit"},
                                         {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                                         {"id": "max", "value": 1}, {"id": "min", "value": 0}]}]),
        panel("barchart", "Alert per acquedotto",
              f"""SELECT coalesce(acquedotto, '—') AS "Acquedotto", count(*) AS "Alert",
                         count(*) FILTER (WHERE is_repeat) AS "di cui ripetuti"
                  FROM v_alert WHERE {TF} AND {ACQ} AND {REP} GROUP BY 1 ORDER BY 2 DESC""", 12, 22, 12, 6,
              options=BAR),
        panel("barchart", "Alert per reparto",
              f"""SELECT reparto AS "Reparto", count(*) AS "Alert" FROM v_alert
                  WHERE {TF} AND {ACQ} AND {REP} GROUP BY 1 ORDER BY 2 DESC""", 12, 28, 6, 6, options=BAR,
              desc="'non mappato' = sito senza Posizione GLPI né riga in config/site_reparto.csv."),
        panel("stat", "Copertura mapping sito → reparto",
              """SELECT siti_mappati::numeric / nullif(siti_totali, 0) AS "Siti mappati",
                        alert_mappati::numeric / nullif(alert_totali, 0) AS "Alert mappati" FROM v_mapping_coverage""",
              18, 28, 6, 6, unit="percentunit",
              desc="Su tutto lo storico. Per migliorarla compilare config/site_reparto.csv.",
              options={"graphMode": "none", "colorMode": "background",
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}},
              field={"min": 0, "max": 1, "thresholds": {"mode": "absolute", "steps": [
                  {"color": BAD, "value": None}, {"color": WARN, "value": 0.5}, {"color": OK, "value": 0.9}]}}),
    ]
    return dashboard("cr-sites", "Control Room – Siti, acquedotti, reparti", p, [VAR_ACQ, VAR_REP])


# ------------------------------------------------------------------ Tempi
def times() -> dict:
    p = freshness()
    note = ("**Tempo di gestione = data di risoluzione − data di apertura** (solvedate, mai closedate: la chiusura è "
            "automatica alle 08:00). Esclusi i ticket senza data di risoluzione. Ora = ora di apertura, Europe/Rome.")
    p += [
        text("Definizione", note, 0, 3, 16, 3),
        panel("stat", "Esclusi dai tempi (senza solvedate)",
              f"SELECT count(*) FILTER (WHERE ttr_min IS NULL) AS \"Esclusi\", count(ttr_min) AS \"Con tempo\" "
              f"FROM v_alert WHERE {TF} AND {ACQ}", 16, 3, 8, 3,
              options={"graphMode": "none", "colorMode": "none", "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}}),
        panel("barchart", "Mediana e p90 per ora di apertura",
              f"""SELECT lpad(h::text, 2, '0') || ':00' AS "Ora",
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min) AS "Mediana",
                         percentile_cont(0.9) WITHIN GROUP (ORDER BY ttr_min) AS "p90",
                         count(ttr_min) AS "n"
                  FROM generate_series(0, 23) h
                  LEFT JOIN v_alert a ON a.hour_local = h AND ttr_min IS NOT NULL AND {TF} AND {ACQ}
                  GROUP BY h ORDER BY h""", 0, 6, 24, 10, unit="m",
              options={**BAR, "xTickLabelRotation": 0},
              overrides=[{"matcher": {"id": "byName", "options": "n"},
                          "properties": [{"id": "custom.hideFrom", "value": {"viz": True, "legend": True, "tooltip": False}},
                                         {"id": "unit", "value": "none"}]}]),
        panel("table", "Mediana e p90 per acquedotto",
              f"""SELECT coalesce(acquedotto, '—') AS "Acquedotto",
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min) AS "Mediana (min)",
                         percentile_cont(0.9) WITHIN GROUP (ORDER BY ttr_min) AS "p90 (min)",
                         count(ttr_min) AS "n"
                  FROM v_alert WHERE ttr_min IS NOT NULL AND {TF} AND {ACQ} GROUP BY 1 ORDER BY 2 DESC""",
              0, 16, 12, 12, field={"decimals": 0}),
        panel("timeseries", "Mediana e p90 settimanali",
              f"""SELECT (date_trunc('week', opened_local) AT TIME ZONE 'Europe/Rome') AS time,
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min) AS "Mediana",
                         percentile_cont(0.9) WITHIN GROUP (ORDER BY ttr_min) AS "p90"
                  FROM v_alert WHERE ttr_min IS NOT NULL AND {TF} AND {ACQ} GROUP BY 1 ORDER BY 1""",
              12, 16, 12, 12, fmt="time_series", unit="m"),
    ]
    return dashboard("cr-times", "Control Room – Tempi di gestione", p, [VAR_ACQ])


# ------------------------------------------------------------------ Esiti
def outcomes() -> dict:
    p = freshness()
    colors = [("Rientrato", OK), ("Manovra", WARN), ("Altro", BLU), ("Non classificato", GRIGIO)]
    ov = [{"matcher": {"id": "byName", "options": n},
           "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]} for n, c in colors]
    p += [
        text("Nota", "Il tipo di soluzione esiste solo per i ticket chiusi dall'app o dopo la sua adozione: lo storico è "
                     "**Non classificato**, e la quota è mostrata esplicitamente.", 0, 3, 16, 3),
        panel("stat", "Quota non classificata nel periodo",
              f"SELECT avg((outcome = 'Non classificato')::int) AS \"Non classificati\" FROM v_alert "
              f"WHERE {TF} AND {ACQ} AND NOT is_open", 16, 3, 8, 3, unit="percentunit",
              options={"graphMode": "none", "colorMode": "none"}),
        panel("timeseries", "Esiti per settimana",
              f"""SELECT (date_trunc('week', opened_local) AT TIME ZONE 'Europe/Rome') AS time, outcome AS metric,
                         count(*) AS value
                  FROM v_alert WHERE {TF} AND {ACQ} AND NOT is_open GROUP BY 1, 2 ORDER BY 1""", 0, 6, 16, 11,
              fmt="time_series", overrides=ov,
              field={"custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal"}}}),
        panel("piechart", "Ripartizione esiti nel periodo",
              f"""SELECT outcome AS "Esito", count(*) AS "Alert" FROM v_alert
                  WHERE {TF} AND {ACQ} AND NOT is_open GROUP BY 1 ORDER BY 2 DESC""", 16, 6, 8, 11, overrides=ov,
              options={"reduceOptions": {"values": True, "calcs": ["lastNotNull"], "fields": ""},
                       "pieType": "donut", "legend": {"showLegend": True, "displayMode": "table", "placement": "bottom",
                                                      "values": ["value", "percent"]}}),
        panel("table", "Esiti per operatore (solo chiusure tracciate dall'app)",
              f"""SELECT handled_by AS "Operatore", count(*) AS "Alert",
                         count(*) FILTER (WHERE outcome = 'Rientrato') AS "Rientrato",
                         count(*) FILTER (WHERE outcome = 'Manovra') AS "Manovra",
                         count(*) FILTER (WHERE outcome = 'Altro') AS "Altro"
                  FROM v_alert WHERE {TF} AND {ACQ} AND handled_by <> 'sconosciuto' GROUP BY 1 ORDER BY 2 DESC""",
              0, 17, 24, 8, desc="Operatore dall'audit dell'app o dalla riga nel testo della soluzione. "
                                 "'Ultima modifica di' di GLPI non è usato."),
    ]
    return dashboard("cr-outcomes", "Control Room – Esiti", p, [VAR_ACQ])


# ------------------------------------------------------------------ Chiamate dal campo
def calls() -> dict:
    p = freshness()
    p += [
        panel("stat", "Chiamate nel periodo", f"SELECT count(*) AS \"Chiamate\" FROM v_call WHERE {TF} AND {ACQ}",
              0, 3, 8, 4, options={"graphMode": "none"}),
        panel("stat", "Tempo di gestione mediano",
              f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min) AS \"Mediana\" FROM v_call "
              f"WHERE ttr_min IS NOT NULL AND {TF} AND {ACQ}", 8, 3, 8, 4, unit="m", options={"graphMode": "none"}),
        panel("stat", "Aperte ora", f"SELECT count(*) AS \"Aperte\" FROM v_call WHERE is_open AND {ACQ}",
              16, 3, 8, 4, options={"graphMode": "none"}),
        panel("timeseries", "Chiamate per settimana",
              f"""SELECT (date_trunc('week', opened_at AT TIME ZONE 'Europe/Rome') AT TIME ZONE 'Europe/Rome') AS time,
                         count(*) AS "Chiamate" FROM v_call WHERE {TF} AND {ACQ} GROUP BY 1 ORDER BY 1""",
              0, 7, 12, 9, fmt="time_series", field={"custom": {"drawStyle": "bars", "fillOpacity": 70}}),
        panel("table", "Chiamate per sito",
              f"""SELECT coalesce(site_code, '—') AS "Sito", count(*) AS "Chiamate",
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY ttr_min) AS "Mediana (min)",
                         max(opened_at) AS "Ultima"
                  FROM v_call WHERE {TF} AND {ACQ} GROUP BY 1 ORDER BY 2 DESC""", 12, 7, 12, 9,
              field={"decimals": 0}),
    ]
    return dashboard("cr-calls", "Control Room – Chiamate dal campo", p, [VAR_ACQ], "now-90d")


ALL = {"overview": overview, "sites": sites, "times": times, "outcomes": outcomes, "calls": calls}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in ALL.items():
        (OUT / f"{name}.json").write_text(json.dumps(fn(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("scritto", OUT / f"{name}.json")


if __name__ == "__main__":
    main()
