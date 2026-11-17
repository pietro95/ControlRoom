"""Parsing dei titoli dei ticket automatici: 'ALERT ON: FNC-IS00600-Casaleno'."""
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
