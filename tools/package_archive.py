#!/usr/bin/env python3
"""Package the tested platform output, preserving Unix modes and app symlinks."""
import os
from pathlib import Path
import stat
import sys
import zipfile

from artifact_manifest import PREFIX, create

ROOT = Path(__file__).resolve().parents[1]


def add(archive, source, name):
    if source.is_symlink():
        entry = zipfile.ZipInfo(name)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, os.readlink(source).encode('utf-8'))
    elif source.is_file():
        archive.write(source, name)
    elif source.is_dir():
        archive.write(source, name + '/')
        for child in sorted(source.iterdir()): add(archive, child, name + '/' + child.name)
    else:
        raise ValueError(f'Missing or unsupported package input: {source}')


def main():
    root_name = 'NSO-GameCube-Controller-Pairing-App'
    if sys.platform == 'darwin':
        target = 'macOS'; sources = [(ROOT / 'dist' / (root_name + '.app'), root_name + '.app')]
    elif sys.platform == 'win32':
        target = 'Windows'; sources = [(ROOT / 'dist' / (root_name + '.exe'), root_name + '.exe')]
    elif sys.platform == 'linux':
        target = 'Linux'; sources = [(ROOT / 'dist' / root_name, root_name)]
        sources += [(ROOT / 'platform/linux' / name, name) for name in
                    ('install.sh', 'controller-256.png', 'nso-gc-controller.desktop')]
    else:
        raise ValueError('Unsupported packaging platform')
    directory = ROOT / 'release-artifacts'; directory.mkdir(exist_ok=False)
    destination = directory / (PREFIX + target + '.zip')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in sources: add(archive, source, name)
    create(destination, ROOT / 'frozen-smoke.json')


if __name__ == '__main__': main()
