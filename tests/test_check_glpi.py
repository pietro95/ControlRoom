"""Verifica delle search option contro il GLPI simulato."""
import asyncio

import httpx

from app.glpi import GlpiClient
from mock import mock_glpi
from sync.check_glpi import check
from sync.config import sync_settings


def run():
    mock_glpi.seed()
    return asyncio.run(check(GlpiClient(transport=httpx.ASGITransport(app=mock_glpi.app))))


def test_default_options_match():
    ok, lines = run()
    assert ok and all(l.startswith("OK") for l in lines[:-1]) and "Ticket nell'entità 14: 7" in lines[-1]


def test_wrong_option_is_reported(monkeypatch):
    monkeypatch.setitem(sync_settings.so, "solved", 16)  # closedate al posto di solvedate
    ok, lines = run()
    bad = [l for l in lines if l.startswith("KO")]
    assert not ok and len(bad) == 1 and "solved" in bad[0] and "glpi_tickets.solvedate" in bad[0]


def test_missing_option_is_reported(monkeypatch):
    monkeypatch.setitem(sync_settings.so, "solution_type", 999)
    ok, lines = run()
    assert not ok and any("(assente)" in l for l in lines)
