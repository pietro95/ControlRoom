"""Reverse proxy verso Grafana con auth proxy.

Grafana non è esposto: ascolta solo sulla rete interna del compose e si fida dell'header
X-WEBAUTH-USER esclusivamente da questo processo (GF_AUTH_PROXY_WHITELIST = IP dell'app).
Il controllo del ruolo "manager" avviene prima di arrivare qui (dipendenza della route).

Regole di sicurezza applicate su ogni richiesta:
- gli header di autenticazione in arrivo dal browser (X-WEBAUTH-*, Authorization, X-Grafana-*)
  sono SEMPRE scartati e X-WEBAUTH-USER è riscritto con l'utente della sessione del portale;
- il cookie di sessione del portale non viene inoltrato a Grafana;
- l'account admin locale di Grafana non è raggiungibile via proxy (username riservato);
- la pagina può essere incorniciata solo dal portale stesso (CSP frame-ancestors 'self').
"""
import logging

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .config import settings

log = logging.getLogger("grafana_proxy")

AUTH_HEADER = "x-webauth-user"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}
# header di richiesta mai inoltrati (oltre agli hop-by-hop)
DROP_REQUEST = {"authorization", "cookie", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
DROP_REQUEST_PREFIX = ("x-webauth-", "x-grafana-")
# header di risposta che il proxy sostituisce
DROP_RESPONSE = {"x-frame-options", "content-security-policy"}
PORTAL_COOKIE = "cr_session"


class GrafanaProxy:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=settings.grafana_url, transport=transport,
            # connessione breve: un container fermo non risponde affatto, la pagina deve dirlo subito
            timeout=httpx.Timeout(settings.grafana_timeout, connect=3.0),
            follow_redirects=False,
        )

    async def aclose(self):
        await self._http.aclose()

    @staticmethod
    def _cookies_without_portal(raw: str) -> str:
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        return "; ".join(p for p in parts if p.split("=", 1)[0].strip() != PORTAL_COOKIE)

    def _request_headers(self, request: Request, username: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in DROP_REQUEST or lk.startswith(DROP_REQUEST_PREFIX):
                continue
            out[k] = v
        cookies = self._cookies_without_portal(request.headers.get("cookie", ""))
        if cookies:
            out["Cookie"] = cookies
        out["X-WEBAUTH-USER"] = username
        return out

    async def forward(self, request: Request, path: str, user: dict):
        username = user["username"].strip().lower()
        if not username or username == settings.grafana_admin_user.strip().lower():
            # un account AD con lo stesso nome dell'admin di Grafana diventerebbe admin: mai
            return JSONResponse({"error": "utente non utilizzabile in Grafana"}, status_code=403)
        url = httpx.URL(path="/grafana/" + path, query=request.url.query.encode("latin-1"))
        req = self._http.build_request(
            request.method, url, headers=self._request_headers(request, username),
            content=request.stream() if request.method not in ("GET", "HEAD") else None,
        )
        try:
            resp = await self._http.send(req, stream=True)
        except httpx.HTTPError as e:
            log.warning("grafana: %s", e.__class__.__name__)
            return JSONResponse({"error": "Statistiche temporaneamente non disponibili (Grafana non raggiungibile)"},
                                status_code=502)
        headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in DROP_RESPONSE]
        r = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
        r.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        if "content-length" in resp.headers and request.method != "HEAD":
            r.raw_headers.append((b"content-length", resp.headers["content-length"].encode()))
        r.raw_headers += [(b"content-security-policy", b"frame-ancestors 'self'"),
                          (b"x-frame-options", b"SAMEORIGIN")]
        return r
