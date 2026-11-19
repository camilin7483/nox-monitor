"""Punto de entrada para las ediciones de escritorio de Nox."""
import json
import os
from pathlib import Path
import secrets
import sys
import tkinter as tk
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / 'app'
PORTABLE = (ROOT / 'portable.mode').is_file()


def launch():
    if os.name != 'nt':
        os.umask(0o077)
    if PORTABLE:
        data = ROOT / 'Datos'
    elif os.name == 'nt':
        data = Path(os.environ['LOCALAPPDATA']) / 'Nox Monitor' / 'Datos'
    else:
        data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'nox-monitor'
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    python_console = Path(sys.executable)
    if python_console.name.lower() == 'pythonw.exe':
        python_console = python_console.with_name('python.exe')
    os.environ.update(
        NOX_PACKAGED='1', NOX_DATA_DIR=str(data), NOX_ASSETS=str(ROOT / 'assets'),
        NOX_NODE_EXECUTABLE=str(ROOT / 'runtime' / ('node.exe' if os.name == 'nt' else 'node')),
        NOX_PYTHON_EXECUTABLE=str(python_console),
        NOX_API_HOST='127.0.0.1', NOX_API_PORT='0',
        PLAYWRIGHT_BROWSERS_PATH=str(data / 'navegadores'),
    )
    os.environ.pop('NOX_API_TOKEN', None)
    os.chdir(data)
    sys.path.insert(0, str(APP))

    from nox_paths import file_lock, save_json
    with file_lock(data / '.setup-lock'):
        config_path = data / 'nox-config.json'
        cfg = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
        if not cfg.get('api_token'):
            cfg.update(api_token=secrets.token_urlsafe(32))
            cfg.setdefault('browser', {'provider': 'bundled'})
            save_json(config_path, cfg)

    from monitor import MonitorApp
    MonitorApp()


if __name__ == '__main__':
    try:
        launch()
    except (ImportError, OSError, ValueError, tk.TclError) as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Nox Monitor', f'No se pudo abrir Nox Monitor.\n\n{error}', parent=root)
        root.destroy()
        sys.exit(1)
