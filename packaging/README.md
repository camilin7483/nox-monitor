# Empaquetar Nox Monitor

El script arma las ediciones instalable y portable desde una lista explícita de archivos de aplicación. El perfil de cada usuario se crea al iniciar la aplicación.

Usa un directorio de trabajo vacío con una carpeta `downloads/`. Prepara estos archivos antes de ejecutar `build.py`.

| Archivo en `downloads/` | Origen |
| --- | --- |
| `node-v24.22.3-linux-x64.tar.xz`, `node-sha.txt` | [Node.js v24.22.3](https://nodejs.org/dist/v24.22.3/). Guarda `SHASUMS256.txt` como `node-sha.txt` y verifica su procedencia. |
| `node-win.exe` | `win-x64/node.exe` de esa misma versión, para Windows. |
| `python-unknown-linux-gnu.tar.gz`, `python-pc-windows-msvc.tar.gz` | [Python Build Standalone 20261110](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110). Usa Python 3.13.18 x86_64, artefactos `install_only_stripped`. |
| `playwright.tgz`, `playwright-core.tgz` | Archivos `resolved` de ambos paquetes en `package-lock.json`. |

La compilación usa Python 3.13, GNU tar y, para Windows, NSIS. El script busca `makensis` en el sistema o en `toolchain/usr/bin/` dentro del directorio de trabajo.

```bash
mkdir -p artefactos
python3 packaging/build.py linux /ruta/trabajo artefactos
python3 packaging/build.py win /ruta/trabajo artefactos
(cd artefactos && sha256sum Nox-Monitor-* > SHA256SUMS.txt)
```

Para repetir una compilación, usa un directorio de trabajo nuevo con las mismas descargas verificadas. Mantén las descargas y la salida fuera del código del repositorio.
