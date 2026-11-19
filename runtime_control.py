"""Controla el proceso de Nox en la edición instalada y portable."""
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from nox_paths import APP_DIR, DATA_DIR, NODE, HIDDEN_CONSOLE, file_lock, save_json

LOCK = DATA_DIR / 'responder.lock'
STOP = DATA_DIR / 'stop-request.json'
RESPONDER = APP_DIR / 'responder.js'


def windows_process_matches(pid, started):
    from ctypes import wintypes
    try:
        stamp = datetime.fromisoformat(started.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return False
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                              wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    api.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        length = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(length.value)
        if not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            return False
        timestamps = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in timestamps)):
            return False
        creation = timestamps[0]
        ticks = (creation.dwHighDateTime << 32) + creation.dwLowDateTime
        return (Path(image.value).resolve() == Path(NODE).resolve() and
                abs(ticks / 10_000_000 - 11_644_473_600 - stamp) < 15)
    finally:
        api.CloseHandle(handle)


def running_process():
    try:
        lock = json.loads(LOCK.read_text(encoding='utf-8'))
        pid, started = lock['pid'], lock.get('startedAt')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not started:
            return None
        if os.name == 'nt':
            return lock if windows_process_matches(pid, started) else None
        proc = Path('/proc') / str(pid)
        argv = (proc / 'cmdline').read_bytes().split(b'\0')
        if len(argv) < 2 or os.fsdecode(argv[1]) != str(RESPONDER):
            return None
        if (proc / 'cwd').resolve(strict=True) != DATA_DIR:
            return None
        return lock
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None


def start():
    if running_process():
        return
    LOCK.unlink(missing_ok=True)
    STOP.unlink(missing_ok=True)
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True})
    with (DATA_DIR / 'startup.log').open('ab') as output:
        bot = subprocess.Popen([NODE, str(RESPONDER)], cwd=DATA_DIR, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=output, **options)
    for _ in range(60):
        if running_process():
            return
        if bot.poll() is not None:
            break
        time.sleep(.1)
    raise RuntimeError('Nox no pudo iniciar. Consulta startup.log en la carpeta de datos.')


def stop():
    instance = running_process()
    if not instance:
        return
    save_json(STOP, {'pid': instance['pid'], 'startedAt': instance['startedAt']})
    for _ in range(100):
        if not running_process():
            STOP.unlink(missing_ok=True)
            return
        time.sleep(.1)
    raise RuntimeError('Nox todavía está cerrando su navegador. Espera unos segundos y reintenta.')


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(DATA_DIR / '.process-lock'):
        action = sys.argv[1] if len(sys.argv) > 1 else ''
        if action in ('stop', 'restart'):
            stop()
        if action in ('start', 'restart'):
            start()
        if action not in ('start', 'stop', 'restart'):
            raise ValueError('Operación desconocida.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
