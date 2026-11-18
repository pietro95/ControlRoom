"""Parsing dei ticket automatici: titolo 'ALERT ON: FNC-IS00600-Casaleno' e corpo (tipo, oggetto, descrizione)."""
import html
import re
from dataclasses import dataclass

_TITLE = re.compile(r"^\s*ALERT\s+ON\s*:\s*([A-Z]{3})-([A-Z0-9]+)\s*-?\s*(.*?)\s*$", re.I)


@dataclass(frozen=True)
class Site:
    acquedotto: str  # prefisso a 3 lettere
    code: str  # es. FNC-IS00600
    name: str  # es. Casaleno

    @property
    def key(self) -> str:
        return self.code


def parse_alert_title(title: str) -> Site | None:
    m = _TITLE.match(title or "")
    if not m:
        return None
    acq, rest, name = m.group(1).upper(), m.group(2).upper(), m.group(3)
    return Site(acq, f"{acq}-{rest}", name)


def clean(s: str) -> str:
    """Toglie caratteri di controllo e spazi in eccesso (input operatore)."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", s or "")).strip()


# ---- corpo del ticket automatico: tipo, oggetto, descrizione ----
# Il corpo arriva da GLPI come HTML (spesso con entità codificate due volte). Si cercano righe "Chiave: valore";
# le chiavi riconosciute sono sinonimi frequenti nelle mail di telecontrollo. Nessuna chiave → descrizione =
# prima riga significativa del testo.
_KEYS = {
    "tipo": ("tipo allarme", "tipo evento", "tipo", "tipologia", "categoria", "classe", "allarme", "evento",
             "alarm type", "type"),
    "oggetto": ("oggetto", "apparato", "apparecchiatura", "dispositivo", "componente", "elemento", "punto",
                "variabile", "tag", "segnale", "item", "host", "object", "device"),
    "descrizione": ("descrizione", "descrizione allarme", "messaggio", "testo", "dettaglio", "problema",
                    "trigger", "description", "message"),
}
_KEY_OF = {syn: field for field, syns in _KEYS.items() for syn in syns}
_LINE = re.compile(r"^\s*([A-Za-zÀ-ÿ ]{2,30}?)\s*[:=]\s*(.+?)\s*$")
_TAG = re.compile(r"<\s*(br|/p|/div|/li|/tr|p|div|li|tr)\b[^>]*>", re.I)


@dataclass(frozen=True)
class AlarmInfo:
    tipo: str
    oggetto: str
    descrizione: str
    luogo: str = ""
    valore: str = ""
    soglia: str = ""


# Formato misure del telecontrollo, anche spezzato su più righe:
# "Serbatoio Cozzo della Guardia - FIT005 (Valore: 177.6375 | Soglia: 169 l/s) - Alert ON Soglia Massima"
#  luogo                          oggetto  valore              soglia              tipo (soglia scattata)
_MEASURE = re.compile(
    r"^(?P<luogo>.+?)\s+-\s+(?P<oggetto>[A-Za-z0-9_./-]+)\s*"
    r"\(\s*Valore\s*:\s*(?P<valore>[^|)]*?)\s*\|\s*Soglia\s*:\s*(?P<soglia>[^)]*?)\s*\)"
    r"\s*-?\s*(?:ALERT(?:\s+(?:ON|OFF))?\b\s*(?P<tipo>.*?))?\s*$", re.I)
# riga "Alert ON <testo>" senza i due punti: il testo dopo ALERT/ALERT ON è il tipo (es. "Soglia Massima").
# "ALERT ON: ..." con i due punti è invece l'eco del titolo e si scarta.
_ALERT_TAIL = re.compile(r"^\s*ALERT(?:\s+(?:ON|OFF)(?![A-Z]))?(?!\s*(?:ON|OFF)\s*:)\s*(?![:A-Z]*:)(.*?)\s*$", re.I)


def _parse_measure(lines: list[str]) -> AlarmInfo | None:
    m = _MEASURE.match(" ".join(lines))
    if not m:
        return None
    luogo, ogg, val, sog, tipo = (clean(m.group(k) or "") for k in ("luogo", "oggetto", "valore", "soglia", "tipo"))
    descr = f"{tipo or 'Allarme'}: valore {val} (soglia {sog})"
    return AlarmInfo(tipo[:300], ogg, descr[:300], luogo[:300], val, sog)


def body_text(content: str) -> list[str]:
    """HTML GLPI -> righe di testo pulite."""
    s = content or ""
    for _ in range(2):  # GLPI 9.x/10 salva il contenuto con entità HTML, a volte doppie
        s = html.unescape(s)
    s = _TAG.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    lines = (clean(x) for x in s.replace("\r", "\n").split("\n"))
    return [x for x in lines if x]


def parse_alert_body(content: str) -> AlarmInfo:
    lines = body_text(content)
    meas = _parse_measure(lines)
    if meas:
        return meas
    found: dict[str, str] = {}
    free: list[str] = []
    for line in lines:
        m = _LINE.match(line)
        field = _KEY_OF.get(m.group(1).strip().lower()) if m else None
        if field:
            found.setdefault(field, m.group(2)[:300])
        elif line.upper().startswith("ALERT"):
            t = _ALERT_TAIL.match(line)
            if t and t.group(1):
                found.setdefault("tipo", t.group(1)[:300])  # "Alert ON Soglia Minima" -> tipo
        else:
            free.append(line)
    descr = found.get("descrizione") or (free[0][:300] if free else "")
    return AlarmInfo(found.get("tipo", ""), found.get("oggetto", ""), descr)


# ---- chiamate dal campo create dall'app: titolo 'CHIAMATA: BLF-PO00100 – nota', corpo con Sito/Chiamante ----
_CALL_TITLE = re.compile(r"^\s*CHIAMATA\s*:\s*(.*?)\s*$", re.I)
_SITE_CODE = re.compile(r"\b([A-Z]{3})-([A-Z0-9]+)\b")


@dataclass(frozen=True)
class CallInfo:
    code: str  # codice sito se riconoscibile, altrimenti ""
    acquedotto: str
    sito: str  # nome completo della posizione (riga "Sito:")
    chiamante: str
    descrizione: str


def parse_call(title: str, content: str) -> CallInfo | None:
    m = _CALL_TITLE.match(title or "")
    if not m:
        return None
    sito = chiamante = ""
    free: list[str] = []
    for line in body_text(content):
        low = line.lower()
        if low.startswith("sito:") and not sito:
            sito = line[5:].strip()
        elif low.startswith("chiamante:") and not chiamante:
            chiamante = line[10:].strip()
        elif not low.startswith("[control room app"):
            free.append(line)
    c = _SITE_CODE.search(m.group(1).upper()) or _SITE_CODE.search(sito.upper())
    code = f"{c.group(1)}-{c.group(2)}" if c else ""
    descr = " · ".join(free)[:300] or m.group(1).split("–", 1)[-1].strip()
    return CallInfo(code, code[:3] if code else "—", sito, chiamante, descr)
