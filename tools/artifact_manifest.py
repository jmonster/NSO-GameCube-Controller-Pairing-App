#!/usr/bin/env python3
"""Create/verify archive integrity and build-input records (not signatures).

Verification binds each archive to an expected checkout and committed native
lock; it does not authenticate a hostile publisher who can replace both files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys

import dependency_lock

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'Linux': 'cp312-linux-x86_64',
    'macOS': 'cp312-darwin-arm64',
    'Windows': 'cp312-win32-x86_64',
}
PREFIX = 'NSO-GameCube-Controller-Pairing-App-'


def checksum(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Artifact is not a regular file: {path}')
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
    return result.hexdigest()


def checked_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a full source commit SHA')
    return value


def lock_metadata(target):
    if target not in TARGETS.values(): raise ValueError('Unsupported release target')
    directory = ROOT / 'requirements' / 'locks' / target
    metadata = dependency_lock.validate(directory, root=ROOT, expected_target=target)
    return metadata, checksum(directory / 'manifest.json')


def validate_smoke(smoke, target):
    platforms = {'cp312-linux-x86_64': ('linux', ('x86_64',)),
                 'cp312-darwin-arm64': ('darwin', ('arm64',)),
                 'cp312-win32-x86_64': ('win32', ('amd64', 'x86_64'))}
    operating_system, architectures = platforms[target]
    required = {'hid', 'usb.core', 'customtkinter', 'tkinter', '_tkinter', 'PIL.Image',
                'Tcl resources', 'Controller resources', 'gc_controller.usb_worker'}
    if (type(smoke) is not dict or smoke.get('frozen') is not True or smoke.get('errors') != [] or
            smoke.get('platform') != operating_system or
            str(smoke.get('architecture', '')).lower() not in architectures or
            not str(smoke.get('python', '')).startswith('3.12.') or
            type(smoke.get('checked')) is not list or not required <= set(smoke['checked'])):
        raise ValueError('Frozen smoke result is missing, failed or for a different target')


def create(archive, smoke_path):
    target = dependency_lock.target()
    name = next((name for name, value in TARGETS.items() if target == value), None)
    if name is None or archive.name != PREFIX + name + '.zip':
        raise ValueError('Release archive name does not match the native target')
    source = checked_sha(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
    subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=ROOT, check=True)
    metadata, lock_hash = lock_metadata(target)
    smoke = json.loads(smoke_path.read_text(encoding='utf-8'))
    validate_smoke(smoke, target)
    record = {'format': 1, 'source_commit': source, 'target': target,
              'artifact': {'name': archive.name, 'sha256': checksum(archive), 'bytes': archive.stat().st_size},
              'lock_manifest_sha256': lock_hash, 'inputs_sha256': metadata['inputs_sha256'],
              'build': {'python': platform.python_version(), 'system': platform.platform(),
                        'distribution_versions': {d['name']: d['version'] for d in metadata['distributions']}},
              'frozen_smoke': smoke,
              'authentication': 'unsigned; verify publisher separately'}
    destination = archive.with_name(archive.name + '.provenance.json')
    if destination.exists() or destination.is_symlink(): raise ValueError('Refusing to replace provenance file')
    destination.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    verify(archive.parent, source, expected_names=[name])
    print(f'Created {destination}')


def verify(directory, source_commit, expected_names=None):
    source_commit = checked_sha(source_commit)
    expected_names = list(TARGETS) if expected_names is None else expected_names
    if not expected_names or len(set(expected_names)) != len(expected_names) or any(n not in TARGETS for n in expected_names):
        raise ValueError('Invalid expected release targets')
    expected_files = set()
    for name in expected_names:
        archive_name = PREFIX + name + '.zip'
        expected_files.update((archive_name, archive_name + '.provenance.json'))
    actual = {p.name for p in directory.iterdir()}
    # A release directory is purpose-built; accepting extra archives is unsafe.
    if actual != expected_files:
        raise ValueError(f'Release file inventory differs: expected={sorted(expected_files)}, actual={sorted(actual)}')
    lines = []
    for name in expected_names:
        target = TARGETS[name]; archive = directory / (PREFIX + name + '.zip')
        record_path = archive.with_name(archive.name + '.provenance.json')
        checksum(record_path)  # Also rejects symlinks/non-files.
        if record_path.stat().st_size > 1024 * 1024: raise ValueError('Oversized provenance record')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        metadata, lock_hash = lock_metadata(target)
        artifact = record.get('artifact', {})
        if (record.get('format') != 1 or record.get('source_commit') != source_commit or
                record.get('target') != target or record.get('lock_manifest_sha256') != lock_hash or
                record.get('inputs_sha256') != metadata['inputs_sha256'] or
                artifact.get('name') != archive.name or type(artifact.get('bytes')) is not int or
                artifact['bytes'] != archive.stat().st_size or artifact.get('sha256') != checksum(archive)):
            raise ValueError(f'Artifact/source/lock verification failed: {archive.name}')
        validate_smoke(record.get('frozen_smoke'), target)
        versions = {d['name']: d['version'] for d in metadata['distributions']}
        if record.get('build', {}).get('distribution_versions') != versions:
            raise ValueError('Provenance dependency inventory differs from lock')
        lines.append(f"{artifact['sha256']}  {archive.name}")
        lines.append(f'{checksum(record_path)}  {record_path.name}')
    return '\n'.join(sorted(lines)) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    make = commands.add_parser('create'); make.add_argument('archive', type=Path); make.add_argument('smoke', type=Path)
    check = commands.add_parser('verify'); check.add_argument('directory', type=Path)
    check.add_argument('--source-commit', required=True); check.add_argument('--target', choices=list(TARGETS), action='append')
    args = parser.parse_args()
    try:
        if args.command == 'create': create(args.archive.absolute(), args.smoke.absolute())
        else: print(verify(args.directory.resolve(), args.source_commit, args.target), end='')
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Artifact verification failed: {exc}\n')


if __name__ == '__main__': main()
