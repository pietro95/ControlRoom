"""deploy.sh durante "install <tag>" cambia la versione di sé stesso (git checkout) mentre bash lo esegue.

Bash legge gli script a mano a mano. Verificato qui:
- git checkout crea un file NUOVO (altro inode): bash continua a leggere il vecchio, quindi l'aggiornamento è sicuro
  anche senza protezioni (test_upgrade_does_not_execute_code_from_the_new_script, sullo script vero);
- una riscrittura SUL POSTO (cp sopra il file, editor) invece farebbe eseguire a bash il contenuto nuovo dalla
  vecchia posizione, a meno che l'ultima riga sia `main "$@"; exit $?` (test_in_place_rewrite_*).
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "scripts" / "deploy.sh").read_text()

STUBS = {
    "docker": """#!/bin/sh
case "$*" in
  *"version --short"*) echo 2.40.3 ;;
  inspect*) echo healthy ;;
esac
exit 0
""",
    "curl": "#!/bin/sh\nexit 0\n",
    "ip": "#!/bin/sh\nexit 0\n",
    "ss": "#!/bin/sh\nexit 0\n",
    "sudo": '#!/bin/sh\n"$@"\n',
}


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def run_upgrade(tmp_path: Path, old_script: str) -> tuple[subprocess.CompletedProcess, Path]:
    spy = tmp_path / "SPIA"
    new_script = old_script + f'\ntouch "{spy}"   # aggiunta nella nuova versione\n' + "# riempimento\n" * 20

    origin = tmp_path / "origin"
    (origin / "scripts").mkdir(parents=True)
    git(origin, "init", "-q", "-b", "main")
    (origin / "scripts" / "deploy.sh").write_text(old_script)
    git(origin, "add", "-A"); git(origin, "commit", "-qm", "v1"); git(origin, "tag", "v1")
    (origin / "scripts" / "deploy.sh").write_text(new_script)
    git(origin, "commit", "-qam", "v2"); git(origin, "tag", "v2")

    base = tmp_path / "base"
    (base / "config").mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(origin), str(base / "app"))
    git(base / "app", "-c", "advice.detachedHead=false", "checkout", "-q", "v1")
    env_lines = {k: "x" for k in ("APP_BIND", "GLPI_URL", "GLPI_APP_TOKEN", "GLPI_USER_TOKEN", "LDAP_URL",
                                  "LDAP_OPERATOR_GROUP_DN", "LDAP_MANAGER_GROUP_DN", "SESSION_SECRET",
                                  "POSTGRES_PASSWORD", "GRAFANA_DB_PASSWORD", "GRAFANA_ADMIN_PASSWORD")}
    env_lines.update(APP_PORT="18480", SYNC_DB_PASSWORD="abc123", PORTAL_PUBLIC_URL="https://t")
    (base / "config" / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_lines.items()))
    os.chmod(base / "config" / ".env", 0o600)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "CR_BASE": str(base)}
    res = subprocess.run(["bash", str(base / "app" / "scripts" / "deploy.sh"), "install", "v2"],
                         env=env, capture_output=True, text=True, timeout=60)
    return res, spy


@pytest.mark.skipif(not shutil.which("git"), reason="git non disponibile")
def test_upgrade_does_not_execute_code_from_the_new_script(tmp_path):
    res, spy = run_upgrade(tmp_path, SCRIPT)
    assert "Installata la versione v2" in res.stdout, res.stdout + res.stderr
    assert res.returncode == 0, res.stderr
    assert not spy.exists(), "bash ha eseguito una riga del deploy.sh della nuova versione"


INPLACE = """main() {
  printf 'echo\\ntouch "%s"\\n' "$SPIA" >> "$0"   # riscrittura sul posto: stesso file, più lungo
  echo "main finito"
}
"""


@pytest.mark.parametrize("last_line,executed", [('main "$@"', True), ('main "$@"; exit $?', False)])
def test_in_place_rewrite_needs_the_guard(tmp_path, last_line, executed):
    script = tmp_path / "s.sh"
    spy = tmp_path / "SPIA"
    script.write_text(INPLACE + last_line + "\n")
    res = subprocess.run(["bash", str(script)], env={**os.environ, "SPIA": str(spy)}, capture_output=True, text=True)
    assert "main finito" in res.stdout
    assert spy.exists() is executed


def test_deploy_script_ends_with_the_guard():
    assert SCRIPT.rstrip("\n").split("\n")[-1] == 'main "$@"; exit $?'
