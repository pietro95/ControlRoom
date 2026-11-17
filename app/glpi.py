"""Client minimale per la REST API legacy di GLPI (apirest.php).

Usa un account di servizio con profilo limitato all'entità Control Room.
L'autore reale (operatore AD) viene scritto nel testo e nell'audit locale.
"""
import asyncio
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("glpi")


class GlpiError(Exception):
    pass


class GlpiClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=f"{settings.glpi_url}/apirest.php",
            timeout=settings.glpi_timeout,
            verify=settings.glpi_verify_tls,
            transport=transport,
        )
        self._session: str | None = None
        self._lock = asyncio.Lock()

    async def aclose(self):
        await self._http.aclose()

    # ---- sessione ----
    def _app_headers(self) -> dict[str, str]:
        return {"App-Token": settings.glpi_app_token}

    async def _init_session(self) -> str:
        r = await self._http.get(
            "/initSession",
            headers={**self._app_headers(), "Authorization": f"user_token {settings.glpi_user_token}"},
        )
        if r.status_code != 200:
            raise GlpiError(f"initSession {r.status_code}")
        tok = r.json()["session_token"]
        h = {**self._app_headers(), "Session-Token": tok}
        r = await self._http.post(
            "/changeActiveEntities",
            headers=h,
            json={"entities_id": settings.entity_id, "is_recursive": False},
        )
        if r.status_code != 200:
            raise GlpiError(f"changeActiveEntities {r.status_code}")
        return tok

    async def _request(self, method: str, path: str, **kw) -> Any:
        for attempt in (1, 2):
            async with self._lock:
                if not self._session:
                    try:
                        self._session = await self._init_session()
                    except httpx.HTTPError as e:
                        raise GlpiError(f"GLPI non raggiungibile: {e.__class__.__name__}") from e
                tok = self._session
            try:
                r = await self._http.request(
                    method, path, headers={**self._app_headers(), "Session-Token": tok}, **kw
                )
            except httpx.HTTPError as e:
                raise GlpiError(f"GLPI non raggiungibile: {e.__class__.__name__}") from e
            if r.status_code == 401 and attempt == 1:
                self._session = None  # sessione scaduta: riprova una volta
                continue
            if r.status_code >= 400:
                raise GlpiError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
            return r.json() if r.content else None
        raise GlpiError("sessione GLPI non ottenibile")

    # ---- lettura ----
    async def search(self, itemtype: str, params: dict) -> tuple[list[dict], int]:
        """Ricerca generica: (righe della pagina, totalcount)."""
        data = await self._request("GET", f"/search/{itemtype}", params=params) or {}
        return data.get("data", []) or [], int(data.get("totalcount", 0) or 0)

    async def open_alerts(self) -> list[dict]:
        """Ticket 'ALERT ON' non risolti dell'entità Control Room."""
        params = {
            "criteria[0][field]": 80, "criteria[0][searchtype]": "equals", "criteria[0][value]": settings.entity_id,
            "criteria[1][link]": "AND", "criteria[1][field]": 12, "criteria[1][searchtype]": "equals",
            "criteria[1][value]": "notold",
            "criteria[2][link]": "AND", "criteria[2][field]": 1, "criteria[2][searchtype]": "contains",
            "criteria[2][value]": "ALERT ON",
            "forcedisplay[0]": 2, "forcedisplay[1]": 1, "forcedisplay[2]": 12, "forcedisplay[3]": 15,
            "forcedisplay[4]": 21,  # descrizione (corpo) del ticket
            "sort": 15, "order": "DESC", "range": "0-999",
        }
        data = await self._request("GET", "/search/Ticket", params=params)
        return [
            {"id": int(r["2"]), "title": r["1"], "status": r.get("12"), "opened": r.get("15"),
             "content": r.get("21") or ""}
            for r in (data or {}).get("data", [])
        ]

    async def search_locations(self, text: str = "", limit: int = 50) -> list[dict]:
        """Posizioni GLPI (nome completo, campo 1); testo vuoto = prime `limit` in ordine alfabetico."""
        params: dict = {"forcedisplay[0]": 2, "forcedisplay[1]": 1, "sort": 1, "order": "ASC", "range": f"0-{limit - 1}"}
        if text:
            params.update({"criteria[0][field]": 1, "criteria[0][searchtype]": "contains", "criteria[0][value]": text})
        data = await self._request("GET", "/search/Location", params=params)
        return [{"id": int(r["2"]), "name": r["1"]} for r in (data or {}).get("data", [])]

    @staticmethod
    def _user_label(first: str | None, last: str | None, login: str | None) -> str:
        full = " ".join(x for x in (last, first) if x)
        return f"{full} ({login})" if full and login else (full or login or "")

    async def search_users(self, text: str = "", limit: int = 50) -> list[dict]:
        """Utenti GLPI attivi per login, cognome o nome (campi 1, 34, 9).

        Criteri piatti in OR (niente gruppi annidati); gli inattivi (campo 8) si scartano qui.
        """
        params: dict = {"forcedisplay[0]": 2, "forcedisplay[1]": 1, "forcedisplay[2]": 34, "forcedisplay[3]": 9,
                        "forcedisplay[4]": 8, "sort": 34, "order": "ASC", "range": f"0-{limit - 1}"}
        if text:
            for i, field in enumerate((34, 9, 1)):
                params.update({f"criteria[{i}][field]": field, f"criteria[{i}][searchtype]": "contains",
                               f"criteria[{i}][value]": text})
                if i:
                    params[f"criteria[{i}][link]"] = "OR"
        data = await self._request("GET", "/search/User", params=params)
        return [{"id": int(r["2"]), "name": self._user_label(r.get("9"), r.get("34"), r.get("1"))}
                for r in (data or {}).get("data", []) if str(r.get("8", 1)) not in ("0", "No", "False")]

    async def get_user(self, user_id: int) -> dict | None:
        try:
            u = await self._request("GET", f"/User/{int(user_id)}")
        except GlpiError as e:
            if "-> 404" in str(e):
                return None
            raise
        if not u or not u.get("is_active", 1) or u.get("is_deleted"):
            return None
        return {"id": int(u["id"]), "name": self._user_label(u.get("firstname"), u.get("realname"), u.get("name"))}

    # ---- scrittura ----
    async def add_solution(self, ticket_id: int, solution_type: int, content: str) -> None:
        await self._request(
            "POST", "/ITILSolution",
            json={"input": {"itemtype": "Ticket", "items_id": ticket_id,
                            "solutiontypes_id": solution_type, "content": content}},
        )

    async def create_call(self, title: str, content: str, location_id: int | None, urgency: int = 3,
                          requester_id: int | None = None) -> int:
        inp: dict[str, Any] = {
            "name": title, "content": content, "entities_id": settings.entity_id,
            "type": 1, "requesttypes_id": settings.request_type_phone, "urgency": urgency,
        }
        if location_id:
            inp["locations_id"] = location_id
        if requester_id:
            inp["_users_id_requester"] = requester_id
        res = await self._request("POST", "/Ticket", json={"input": inp})
        return int(res["id"])

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/getFullSession")
            return True
        except GlpiError:
            return False
