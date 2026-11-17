"""Grafana finto (solo test): restituisce ciò che riceve, per verificare cosa inoltra il proxy."""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()


@app.api_route("/grafana/{path:path}", methods=["GET", "HEAD", "POST"])
async def echo(path: str, request: Request):
    if path == "api/health":
        return {"database": "ok"}
    if path == "page.html":  # pagina HTML: il proxy aggiunge il tema del portale
        return HTMLResponse("<html><head><title>g</title></head><body>"
                            + request.headers.get("accept-encoding", "") + "</body></html>")
    body = (await request.body()).decode()
    return JSONResponse(
        {"path": path, "query": request.url.query, "method": request.method, "body": body,
         "headers": {k.lower(): v for k, v in request.headers.items()}},
        headers={"X-Frame-Options": "deny", "Set-Cookie": "grafana_session=abc; Path=/grafana"},
    )
