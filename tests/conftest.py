"""Configurazione comune dei test. Nessun test usa la rete: i dati sono simulati."""
import os
import socket
import sys
import tempfile

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RADICE)

# Lo stato e il registro dei test vanno in una cartella temporanea, mai in /data.
os.environ.setdefault("STATO_PATH", os.path.join(tempfile.mkdtemp(), "stato.json"))

import pytest  # noqa: E402


def _porta_libera():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bot():
    """Carica main.py SENZA avviare scheduler e ciclo infinito (tutto prima di '# ── Main ──').
    Restituisce lo spazio dei nomi con funzioni e variabili del bot."""
    os.environ["PORT"] = str(_porta_libera())
    src = open(os.path.join(RADICE, "main.py"), encoding="utf-8").read().split("# ── Main ──")[0]
    ns = {"__name__": "main_test", "__file__": os.path.join(RADICE, "main.py")}
    exec(compile(src, "main.py", "exec"), ns)
    return ns
