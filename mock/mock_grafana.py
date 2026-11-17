"""Grafana finto (solo test): restituisce ciò che riceve, per verificare cosa inoltra il proxy."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.api_route("/grafana/{path:path}", methods=["GET", "HEAD", "POST"])
async def echo(path: str, request: Request):
    if path == "api/health":
        return {"database": "ok"}
    body = (await request.body()).decode()
    return JSONResponse(
        {"path": path, "query": request.url.query, "method": request.method, "body": body,
         "headers": {k.lower(): v for k, v in request.headers.items()}},
        headers={"X-Frame-Options": "deny", "Set-Cookie": "grafana_session=abc; Path=/grafana"},
    )
