"""Ambiente di test: impostato prima di qualunque import di app.* (Settings legge l'ambiente all'import)."""
import os
import tempfile

os.environ.update(DEV_AUTH="1", GLPI_APP_TOKEN="a", GLPI_USER_TOKEN="u",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "audit.db"))
