"""DATASET SINTETICO per provare dashboard e KPI. NON sono dati reali e non vanno caricati in produzione.

Uso (database di sviluppo, con lo schema già applicato dal worker o da questo script):
    DATABASE_URL=postgresql://cr_sync:...@localhost:5432/controlroom python -m scripts.gen_synthetic [--seed 42]

Il generatore crea i ticket nel GLPI SIMULATO (mock/mock_glpi.py, in memoria) e poi esegue il VERO worker di sync,
quindi i dati passano per lo stesso percorso di produzione (date in ora di Roma, classificazione, esclusioni, viste).
In v_sync_status.dataset_label resta scritto "SINTETICO ...": le dashboard lo mostrano in evidenza.

PARAMETRI PRESI DALLE CIFRE FORNITE (ordine di grandezza, non riproduzione):
- ~4.194 ticket nell'entità, di cui 194 "ALERT OFF" storici;
- ~800 ticket con Posizione (quindi con reparto deducibile);
- ~50% di alert ripetuti entro 60 minuti sullo stesso sito;
- 12 acquedotti: ANC, FNC, MOE, BLF, DSG, FAV, MAW, VIG, EGD, MAE, ALC, MOW;
- tipo soluzione presente solo dopo l'adozione dell'app; chiusura automatica alle 08:00 dopo la risoluzione;
- chiusura massiva il 20/08/2026 alle 17:42.

PARAMETRI INVENTATI (nessun dato reale dietro: servono solo a rendere i grafici leggibili):
- periodo 01/01/2026 - 15/11/2026; adozione dell'app il 01/11/2026;
- 8-12 siti per acquedotto, nomi "Sito NNN", frequenza degli alert per sito con legge di Zipf (s=1.1);
- profilo orario con picco diurno; tempo di gestione log-normale (mediana 25 min) x1.5 di notte, x0.7-1.4 per acquedotto;
- probabilità 0.5 che un alert generi una ripetizione dopo 2-55 minuti;
- 160 ticket lasciati aperti e chiusi in massa il 20/08/2026 17:42;
- esiti dopo l'adozione: Rientrato 65%, Manovra 25%, Altro 10%; operatori "Operatore A/B/C";
- reparti "SINT Nord/Centro/Sud/Ovest" (prefisso SINT per renderli riconoscibili);
- 5 ticket "Prova", 3 ticket con titolo libero, 12 chiamate dal campo dopo l'adozione;
- ticket aperti negli ultimi 2 giorni: 30% ancora aperti.
"""
import argparse
import asyncio
import math
import random
from datetime import datetime, timedelta

import httpx
import psycopg

from app.config import settings
from app.glpi import GlpiClient
from mock import mock_glpi
from sync import worker
from sync.config import sync_settings
from sync.rules import ROME

ACQ = ["ANC", "FNC", "MOE", "BLF", "DSG", "FAV", "MAW", "VIG", "EGD", "MAE", "ALC", "MOW"]
REPARTI = ["SINT Nord", "SINT Centro", "SINT Sud", "SINT Ovest"]
START = datetime(2026, 1, 1, tzinfo=ROME)
END = datetime(2026, 11, 15, 23, 59, tzinfo=ROME)
ADOPTION = datetime(2026, 11, 1, tzinfo=ROME)
MASS = datetime(2026, 8, 20, 17, 42, tzinfo=ROME)
TARGET_TOTAL, N_OFF, N_PROVA, N_OTHER, N_CALL, N_LOC, N_MASS = 4194, 194, 5, 3, 12, 800, 160
HOUR_W = [2, 1.5, 1.2, 1, 1, 1.2, 2, 3, 4, 4.5, 4.5, 4, 3.5, 3.5, 4, 4, 3.8, 3.5, 3, 2.8, 2.6, 2.4, 2.2, 2]
OPERATORS = [("Operatore A", "opa"), ("Operatore B", "opb"), ("Operatore C", "opc")]
LABEL = "SINTETICO (scripts/gen_synthetic.py, seed {seed}) - NON DATI REALI"


def fmt(dt: datetime) -> str:
    return dt.astimezone(ROME).strftime("%Y-%m-%d %H:%M:%S")


def next_0800(dt: datetime) -> datetime:
    d = dt.astimezone(ROME)
    c = d.replace(hour=8, minute=0, second=0, microsecond=0)
    return c if c > d else (c + timedelta(days=1))


def build(seed: int = 42) -> list[dict]:
    """Elenco dei ticket sintetici (dizionari per mock_glpi.add_ticket). Deterministico per seed."""
    rnd = random.Random(seed)
    sites = []
    for acq in ACQ:
        reparto = rnd.choice(REPARTI)
        for _ in range(rnd.randint(8, 12)):
            sites.append({"code": f"{acq}-PA{rnd.randint(100, 9999):05d}", "name": f"Sito {len(sites) + 1:03d}",
                          "acq": acq, "reparto": reparto})
    rnd.shuffle(sites)
    weights = [1 / (i + 1) ** 1.1 for i in range(len(sites))]
    acq_factor = {a: rnd.uniform(0.7, 1.4) for a in ACQ}
    span = (END - START).total_seconds()

    def random_time(lo: datetime = START, hi: datetime = END) -> datetime:
        while True:
            t = lo + timedelta(seconds=rnd.random() * (hi - lo).total_seconds())
            if rnd.random() < HOUR_W[t.hour] / max(HOUR_W):
                return t.replace(microsecond=0)

    n_alert = TARGET_TOTAL - N_OFF - N_PROVA - N_OTHER - N_CALL
    alerts = []
    while len(alerts) < n_alert:
        site = rnd.choices(sites, weights)[0]
        t = random_time()
        alerts.append((site, t))
        while rnd.random() < 0.5 and len(alerts) < n_alert:
            t = t + timedelta(minutes=rnd.uniform(2, 55))
            if t > END:
                break
            alerts.append((site, t.replace(microsecond=0)))
    alerts.sort(key=lambda x: x[1])

    out: list[dict] = []
    mass_pool = [i for i, (_, t) in enumerate(alerts) if datetime(2026, 7, 1, tzinfo=ROME) <= t < MASS]
    mass_ids = set(rnd.sample(mass_pool, N_MASS))
    loc_ids = set(rnd.sample(range(len(alerts)), N_LOC))
    for i, (site, t) in enumerate(alerts):
        tk = {"name": f"ALERT ON: {site['code']}-{site['name']}", "date": fmt(t)}
        if i in loc_ids:
            tk["location"] = f"{site['reparto']} > {site['code']} {site['name']}"
            tk["status"] = 2
        if i in mass_ids:
            solved = closed = MASS + timedelta(seconds=rnd.randint(0, 59))
        elif t > END - timedelta(days=2) and rnd.random() < 0.3:
            out.append(tk)
            continue
        else:
            night = 1.5 if t.hour < 7 or t.hour >= 21 else 1.0
            ttr = math.exp(rnd.gauss(math.log(25), 1.0)) * night * acq_factor[site["acq"]]
            solved = t + timedelta(minutes=max(1.0, ttr))
            closed = next_0800(solved)
        tk.update(solvedate=fmt(solved), status=6 if closed <= END else 5)
        if closed <= END:
            tk["closedate"] = fmt(closed)
        if solved >= ADOPTION and i not in mass_ids:
            kind = rnd.choices(["Allarme rientrato", "Manovra eseguita", "Altro"], [65, 25, 10])[0]
            name, user = rnd.choice(OPERATORS)
            tk.update(solutiontype=kind, solution=f"{kind}.\n[Control Room app — operatore: {name} ({user})]")
        out.append(tk)

    for _ in range(N_OFF):
        site = rnd.choices(sites, weights)[0]
        t = random_time(START, datetime(2026, 6, 1, tzinfo=ROME))
        out.append({"name": f"ALERT OFF: {site['code']}-{site['name']}", "date": fmt(t), "status": 6,
                    "solvedate": fmt(t + timedelta(minutes=5)), "closedate": fmt(next_0800(t))})
    for k in range(N_PROVA):
        t = random_time()
        out.append({"name": f"Prova invio {k + 1} ALERT ON: FNC-IS00400-Test", "date": fmt(t), "status": 6,
                    "solvedate": fmt(t + timedelta(minutes=1)), "closedate": fmt(next_0800(t))})
    for k in range(N_OTHER):
        out.append({"name": f"Richiesta varia {k + 1}", "date": fmt(random_time())})
    for _ in range(N_CALL):
        site = rnd.choice(sites)
        t = random_time(ADOPTION, END)
        solved = t + timedelta(minutes=rnd.uniform(15, 240))
        name, user = rnd.choice(OPERATORS)
        out.append({"name": f"CHIAMATA: {site['code']} – {rnd.choice(['Mancanza energia', 'Guasto idraulico / perdita', 'Intrusione / sicurezza'])}",
                    "date": fmt(t), "requesttype": "Telefono", "status": 6, "solvedate": fmt(solved),
                    "closedate": fmt(next_0800(solved)), "location": f"{site['reparto']} > {site['code']} {site['name']}",
                    "solution": f"Altro: intervento squadra.\n[Control Room app — operatore: {name} ({user})]",
                    "solutiontype": "Altro"})
    for t in out:
        t["date_mod"] = max(x for x in (t["date"], t.get("solvedate"), t.get("closedate")) if x)
    return out


async def load(conn: psycopg.Connection, seed: int) -> dict:
    mock_glpi.seed()
    mock_glpi.STATE["tickets"].clear()
    for t in build(seed):
        mock_glpi.add_ticket(**t)
    # credenziali del GLPI simulato (in memoria, nessuna rete): indipendenti da quelle del .env
    saved = (settings.glpi_app_token, settings.glpi_user_token, settings.entity_id)
    settings.glpi_app_token, settings.glpi_user_token, settings.entity_id = "a", "u", 14
    glpi = GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))
    try:
        rep = await worker.run_once(conn, glpi)
    finally:
        await glpi.aclose()
        settings.glpi_app_token, settings.glpi_user_token, settings.entity_id = saved
    if not rep.get("ok"):
        raise SystemExit(f"caricamento sintetico fallito: {rep}")
    worker.set_state(conn, "dataset_label", LABEL.format(seed=seed))
    return rep


async def replace_with_synthetic(conn: psycopg.Connection, seed: int = 42, force: bool = False) -> dict:
    """Svuota il reporting e carica il dataset sintetico. Rifiuta se il DB contiene dati non sintetici."""
    label = worker.get_state(conn, "dataset_label")
    n = conn.execute("SELECT count(*) FROM ticket").fetchone()[0]
    if n and not (label or "").startswith("SINTETICO") and not force:
        raise SystemExit(f"Il database contiene {n} ticket NON sintetici: rifiuto di sovrascriverli (usa --force).")
    with conn.transaction():
        conn.execute("TRUNCATE ticket, handler, excluded_ticket, sync_state")
    for k in ("audit_db_path", "site_reparto_csv", "excluded_csv"):
        setattr(sync_settings, k, "")
    return await load(conn, seed)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="svuota anche un database che contiene dati non sintetici")
    args = ap.parse_args()
    conn = worker.connect()
    worker.apply_schema(conn)
    print(asyncio.run(replace_with_synthetic(conn, args.seed, args.force)))


if __name__ == "__main__":
    main()
