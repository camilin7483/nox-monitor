"""Instalador gráfico de Nox Monitor por usuario."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import font

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
DEST = BASE / 'nox-monitor-app'


def desktop_value(path):
    value = str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    return '"' + value + '"'


def install():
    BASE.mkdir(parents=True, exist_ok=True)
    if DEST.exists() and not (DEST / '.nox-installation').is_file():
        raise OSError(f'La carpeta {DEST} ya existe y pertenece a otra aplicación.')
    with tempfile.TemporaryDirectory(dir=BASE, prefix='.nox-install-') as temp:
        staging = Path(temp) / 'new'
        old = Path(temp) / 'previous'
        shutil.copytree(ROOT, staging, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', 'portable.mode'))
        (staging / '.nox-installation').touch()
        if DEST.exists():
            DEST.rename(old)
        try:
            staging.rename(DEST)
        except OSError:
            if old.exists():
                old.rename(DEST)
            raise
    folder = BASE / 'applications'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'nox-monitor.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Nox Monitor\n'
        'Comment=Monitor y asistente de WhatsApp\n'
        f'Exec={desktop_value(DEST / "Nox Monitor")}\nIcon={DEST / "assets/nox.png"}\n'
        'Terminal=false\nCategories=Network;\n', encoding='utf-8')


def launch():
    subprocess.Popen([str(DEST / 'Nox Monitor')], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wizard():
    root = tk.Tk()
    root.title('Instalar Nox Monitor')
    root.geometry('720x460')
    root.resizable(False, False)
    root.configure(bg='#171c18')
    installed_fonts = {family.lower(): family for family in font.families(root)}
    family = next((installed_fonts[name] for name in ('inter', 'dejavu sans', 'liberation sans', 'noto sans')
                   if name in installed_fonts), font.nametofont('TkDefaultFont').actual('family'))
    logo = tk.PhotoImage(file=ROOT / 'assets/nox.png')
    banner = tk.PhotoImage(file=ROOT / 'assets/banner.png')
    root.iconphoto(True, logo)
    tk.Label(root, image=banner, bd=0, bg='#171c18').pack(side='left', fill='y')
    body = tk.Frame(root, bg='#171c18', padx=26, pady=26)
    body.pack(side='left', fill='both', expand=True)

    def label(text, size, foreground, bold=False):
        return tk.Label(body, text=text, fg=foreground, bg='#171c18', justify='left',
                        wraplength=420, font=(family, size, 'bold' if bold else 'normal'))

    label('NOX MONITOR', 10, '#b8df96', True).pack(anchor='w', pady=(8, 14))
    label('Instala Nox Monitor', 25, '#edf4e9', True).pack(anchor='w')
    label('Vincula WhatsApp en una ventana privada. Nox guarda tu sesión en este equipo.',
          13, '#edf4e9').pack(anchor='w', pady=(14, 10))
    label(f'Ubicación de instalación\n{DEST}', 10, '#aebbac').pack(anchor='w', pady=(8, 0))
    status = label('', 10, '#b8df96')
    status.pack(anchor='w', pady=(15, 0))
    actions = tk.Frame(body, bg='#171c18')
    actions.pack(side='bottom', anchor='w', fill='x')

    def finished(error):
        root.protocol('WM_DELETE_WINDOW', root.destroy)
        if error:
            action.configure(text='Reintentar', command=start, state='normal')
            status.configure(text=error, fg='#ffc0b0')
            return
        status.configure(text='Instalación completada. Nox ya está en el menú de aplicaciones.')
        try:
            launch()
        except OSError as error:
            status.configure(text=f'Instalación completada. No se pudo abrir Nox: {error}', fg='#ffc0b0')
            action.configure(text='Cerrar', command=root.destroy, state='normal')
        else:
            root.after(700, root.destroy)

    def start():
        action.configure(state='disabled')
        cancel.configure(state='disabled')
        root.protocol('WM_DELETE_WINDOW', lambda: None)
        status.configure(text='Instalando…', fg='#b8df96')
        def work():
            try:
                install()
            except OSError as error:
                root.after(0, finished, str(error))
            else:
                root.after(0, finished, '')
        threading.Thread(target=work, daemon=True).start()

    action = tk.Button(actions, text='Instalar y abrir Nox', command=start, bg='#b8df96', fg='#1a2619',
                       activebackground='#d3ecbd', padx=15, pady=8, bd=0, font=(family, 11, 'bold'))
    action.pack(side='left')
    cancel = tk.Button(actions, text='Cancelar', command=root.destroy, bg='#2c372c', fg='#edf4e9',
                       activebackground='#3a483a', padx=15, pady=8, bd=0, font=(family, 11))
    cancel.pack(side='left', padx=8)
    root.mainloop()


if __name__ == '__main__':
    if sys.argv[1:] == ['--silent']:
        install()
    else:
        wizard()
