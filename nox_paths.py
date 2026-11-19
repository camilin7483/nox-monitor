"""Rutas y utilidades de ejecución del monitor de Nox."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('NOX_DATA_DIR') or APP_DIR).resolve()
PACKAGED = DATA_DIR != APP_DIR or os.environ.get('NOX_PACKAGED') == '1'
NODE = os.environ.get('NOX_NODE_EXECUTABLE', 'node')
HIDDEN_CONSOLE = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as tmp:
        filename = Path(tmp.name)
        try:
            with tmp:
                json.dump(value, tmp, ensure_ascii=False, indent=2)
            filename.replace(path)
        finally:
            filename.unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
