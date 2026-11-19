"""Arma las distribuciones desde binarios oficiales verificados y una lista cerrada de archivos."""
import argparse
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

SOURCE = Path(__file__).resolve().parent.parent
VERSION = '1.0.0'
NODE_VERSION = 'v24.22.3'
PYTHON_HASHES = {
    'linux': ('unknown-linux-gnu', 'f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb'),
    'win': ('pc-windows-msvc', '2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805'),
}
APP = ['monitor.py','assistant_ui.py','assistant.js','responder.js','api.js','db.js',
       'nox_paths.py','runtime_control.py','runtime-paths.js','transcribe.py','plugins/index.js']


def checksum(path, algorithm):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def verified(path, digest):
    if checksum(path, 'sha256') != digest:
        raise ValueError(f'La suma SHA-256 no coincide para {path.name}')
    return path


def archive(source, target):
    subprocess.run(['tar', '-czf', str(target), '-C', str(source), '.'], check=True)


def build(platform, work, outputs):
    import json
    stage = work / f'stage-{platform}'
    stage.mkdir(exist_ok=False)
    app = stage / 'app'
    vendor = stage / 'runtime'
    assets = stage / 'assets'
    licences = stage / 'licencias'
    packaging = stage / 'packaging'
    for folder in (app, vendor, assets, licences, packaging):
        folder.mkdir()
    for relative in APP:
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / relative, target)
    for name in ['banner.png','nox.png','nox.ico']:
        shutil.copyfile(SOURCE / 'packaging/assets' / name, assets / name)
    shutil.copyfile(SOURCE / 'packaging/launcher.py', packaging / 'launcher.py')

    downloads = work / 'downloads'
    locks = json.loads((SOURCE / 'package-lock.json').read_text(encoding='utf-8'))['packages']
    for package in ['playwright','playwright-core']:
        compressed = downloads / (package + '.tgz')
        with compressed.open('rb') as archive_file:
            integrity = 'sha512-' + base64.b64encode(hashlib.file_digest(archive_file, 'sha512').digest()).decode()
        if integrity != locks['node_modules/' + package]['integrity']:
            raise ValueError(f'La integridad del paquete {package} no coincide')
        dest = app / 'node_modules' / package
        dest.mkdir(parents=True)
        subprocess.run(['tar', '-xzf', str(compressed), '-C', str(dest), '--strip-components=1'],check=True)

    target, digest = PYTHON_HASHES[platform]
    python_tar = verified(downloads / f'python-{target}.tar.gz', digest)
    python_dir = vendor / 'python'
    python_dir.mkdir()
    subprocess.run(['tar','-xzf',str(python_tar),'-C',str(python_dir),'--strip-components=1'],check=True)
    sums = dict(line.split(maxsplit=1)[::-1] for line in (downloads/'node-sha.txt').read_text().splitlines() if line.strip())
    linux_node = f'node-{NODE_VERSION}-linux-x64'
    node_archive = verified(downloads / (linux_node + '.tar.xz'), sums[linux_node + '.tar.xz'])
    with tarfile.open(node_archive) as node_bundle:
        (licences/'Node.js.txt').write_bytes(node_bundle.extractfile(linux_node + '/LICENSE').read())
        if platform == 'win':
            node = next((downloads/name for name in ('node-win-verified.exe','node-win.exe')
                         if (downloads/name).exists() and checksum(downloads/name, 'sha256') == sums['win-x64/node.exe']), None)
            if node is None:
                raise ValueError('Falta el ejecutable de Node.js para Windows verificado')
            shutil.copyfile(node, vendor/'node.exe')
        else:
            (vendor/'node').write_bytes(node_bundle.extractfile(linux_node + '/bin/node').read())
            (vendor/'node').chmod(0o755)

    for candidate in (python_dir / 'LICENSE.txt', python_dir / 'LICENSE'):
        if candidate.is_file():
            shutil.copyfile(candidate, licences / 'Python.txt')
            break
    shutil.copyfile(SOURCE / 'packaging/LEEME.txt', stage / 'LEEME.txt')
    if platform == 'win':
        nsis = work / 'toolchain/usr/bin/makensis'
        env = os.environ.copy()
        if nsis.is_file():
            env['NSISDIR'] = str(work / 'toolchain/usr/share/nsis')
        else:
            nsis = shutil.which('makensis')
            if nsis is None:
                raise FileNotFoundError('Instala NSIS para compilar la edición de Windows')
        art = SOURCE / 'packaging/assets'

        def make(script, out, extra=()):
            subprocess.run([str(nsis), '-V2', f'-DART={art}', f'-DOUT={out}', *extra,
                            str(SOURCE / 'packaging' / script)], env=env, check=True)
        make('windows-launcher.nsi', stage / 'Nox Monitor.exe')
        make('windows-setup.nsi', outputs / f'Nox-Monitor-{VERSION}-Windows-x64-Instalador.exe',
             [f'-DPAYLOAD={stage}'])
        (stage/'portable.mode').touch()
        with zipfile.ZipFile(outputs/f'Nox-Monitor-{VERSION}-Windows-x64-Portable.zip','w',
                             compression=zipfile.ZIP_DEFLATED,compresslevel=6) as result:
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    result.write(path, 'Nox Monitor/' + path.relative_to(stage).as_posix())
    else:
        shutil.copyfile(SOURCE / 'packaging/nox-linux', stage / 'Nox Monitor')
        (stage/'Nox Monitor').chmod(0o755)
        shutil.copyfile(SOURCE / 'packaging/linux_install.py', packaging / 'linux_install.py')
        payload = work / 'linux-payload.tar.gz'
        archive(stage, payload)
        setup = outputs / f'Nox-Monitor-{VERSION}-Linux-x64-Instalador.run'
        with setup.open('wb') as result, payload.open('rb') as bundle:
            result.write((SOURCE / 'packaging/linux-setup.sh').read_bytes())
            shutil.copyfileobj(bundle, result)
        setup.chmod(0o755)
        (stage/'portable.mode').touch()
        subprocess.run(['tar','-czf',str(outputs/f'Nox-Monitor-{VERSION}-Linux-x64-Portable.tar.gz'),
                        '--transform=s,^\\.,Nox Monitor,', '-C', str(stage), '.'], check=True)
    return stage


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('platform', choices=('linux','win'))
    parser.add_argument('work', type=Path)
    parser.add_argument('outputs', type=Path)
    args = parser.parse_args()
    print(build(args.platform, args.work.resolve(), args.outputs.resolve()))
