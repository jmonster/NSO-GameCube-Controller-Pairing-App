#!/usr/bin/env python3
"""Generate reviewable native hash locks or install them in a fresh build venv.

Candidate generation deliberately resolves new versions. Installation never
resolves an unlocked fallback and disables isolated, unpinned source-build deps.
Locks cover Python distributions, not the OS image, SDK, compiler or system libs.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
import venv
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = {'pip', 'setuptools', 'wheel', 'packaging'}


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def target():
    machine = platform.machine().lower()
    machine = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(machine, machine)
    return f'cp{sys.version_info.major}{sys.version_info.minor}-{sys.platform}-{machine}'


def inputs(root=ROOT):
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    return {'python': project['project']['requires-python'],
            'requirements': sorted(set(project['project']['dependencies'] +
                                      project['project']['optional-dependencies']['build'] +
                                      project['build-system']['requires'] + sorted(BOOTSTRAP)))}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(root=ROOT):
    return digest(json.dumps(inputs(root), sort_keys=True).encode())


def entries_from_report(report):
    entries = {}
    for item in report['install']:
        name = canonical(item['metadata']['name'])
        version = item['metadata']['version']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or not re.fullmatch(r'[A-Za-z0-9.!+_-]+', version):
            raise ValueError('Invalid distribution metadata')
        download = item['download_info']
        sha = download.get('archive_info', {}).get('hashes', {}).get('sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError(f'{name}: missing archive SHA256 (VCS/editable inputs are not hash-lockable)')
        url = download['url']; parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError(f'{name}: unsafe distribution URL')
        if item.get('is_direct'):
            if not re.fullmatch(r'https://github.com/yannbouteiller/vgamepad/archive/[0-9a-f]{40}\.tar\.gz', url):
                raise ValueError(f'{name}: unapproved direct source')
            requirement = f'{name} @ {url}'
        else:
            if parsed.hostname != 'files.pythonhosted.org':
                raise ValueError(f'{name}: unexpected registry download host')
            requirement = f'{name}=={version}'
        if name in entries:
            raise ValueError(f'{name}: duplicate resolved distribution')
        entries[name] = {'name': name, 'version': version, 'url': url,
                         'sha256': sha, 'requirement': requirement}
    if not BOOTSTRAP <= entries.keys():
        raise ValueError('Incomplete build-tool bootstrap')
    return entries


def render(entries):
    return ('# Generated native dependency lock; review changes before use.\n' + ''.join(
        f"{entry['requirement']} \\\n    --hash=sha256:{entry['sha256']}\n"
        for entry in sorted(entries, key=lambda item: item['name']))).encode()


def generate(output):
    destination = output / target()
    destination.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ, VGAMEPAD_SKIP_VIGEMBUS_INSTALL='true', PIP_CONFIG_FILE=os.devnull)
    with tempfile.TemporaryDirectory() as directory:
        request = Path(directory) / 'requirements.in'
        report_path = Path(directory) / 'report.json'
        request.write_text('\n'.join(inputs()['requirements']) + '\n', encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install',
                        '--index-url', 'https://pypi.org/simple', 'setuptools', 'wheel', 'packaging'],
                       env=environment, check=True, timeout=300)
        subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install',
                        '--index-url', 'https://pypi.org/simple', '--dry-run', '--ignore-installed',
                        '--no-build-isolation', '--report', str(report_path), '-r', str(request)],
                       env=environment, check=True, timeout=600)
        entries = entries_from_report(json.loads(report_path.read_text(encoding='utf-8')))
    for name, selected in [('requirements.lock', entries.values()),
                           ('bootstrap.lock', [entries[n] for n in BOOTSTRAP])]:
        (destination / name).write_bytes(render(selected))
    metadata = {'format': 1, 'target': target(), 'inputs_sha256': fingerprint(),
                'files': {name: digest((destination / name).read_bytes())
                          for name in ('requirements.lock', 'bootstrap.lock')},
                'distributions': sorted(entries.values(), key=lambda item: item['name'])}
    (destination / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(f'Generated {destination}; this candidate must be reviewed and tested before publication.')


def validate(directory, root=ROOT, *, expected_target=None):
    if directory.is_symlink() or any((directory / name).is_symlink() for name in
                                     ('manifest.json', 'bootstrap.lock', 'requirements.lock')):
        raise ValueError('Dependency locks must be regular checked-in files')
    metadata = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if metadata.get('format') != 1 or metadata.get('target') != (target() if expected_target is None else expected_target):
        raise ValueError('Dependency lock does not match this Python/platform/architecture')
    if metadata.get('inputs_sha256') != fingerprint(root):
        raise ValueError('Dependency manifest changed; regenerate and review the native lock')
    if set(metadata.get('files', {})) != {'bootstrap.lock', 'requirements.lock'}:
        raise ValueError('Invalid lock file inventory')
    for name, expected in metadata['files'].items():
        if digest((directory / name).read_bytes()) != expected:
            raise ValueError(f'Dependency lock checksum mismatch: {name}')
    distributions = metadata['distributions']
    checked = entries_from_report({'install': [
        {'metadata': {'name': entry['name'], 'version': entry['version']},
         'download_info': {'url': entry['url'], 'archive_info': {'hashes': {'sha256': entry['sha256']}}},
         'is_direct': ' @ ' in entry['requirement']} for entry in distributions]})
    if sorted(checked.values(), key=lambda item: item['name']) != distributions:
        raise ValueError('Noncanonical or unsafe dependency inventory')
    by_name = {entry['name']: entry for entry in distributions}
    if len(by_name) != len(distributions) or not BOOTSTRAP <= by_name.keys():
        raise ValueError('Invalid dependency inventory')
    if render(distributions) != (directory / 'requirements.lock').read_bytes():
        raise ValueError('Requirements and dependency inventory differ')
    if render([by_name[n] for n in BOOTSTRAP]) != (directory / 'bootstrap.lock').read_bytes():
        raise ValueError('Bootstrap and dependency inventory differ')
    return metadata


def install(locks, environment_path, github_path=False):
    lock = locks / target()
    validate(lock)
    if environment_path.exists():
        raise ValueError('Build environment already exists; select a fresh --venv directory')
    venv.EnvBuilder(with_pip=True).create(environment_path)
    binary = environment_path / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    env = dict(os.environ, VGAMEPAD_SKIP_VIGEMBUS_INSTALL='true', PIP_CONFIG_FILE=os.devnull)
    base = [str(binary), '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple']
    subprocess.run(base + ['--require-hashes', '--only-binary=:all:', '-r', str(lock / 'bootstrap.lock')],
                   env=env, check=True, timeout=300)
    subprocess.run(base + ['--require-hashes', '--no-build-isolation', '-r', str(lock / 'requirements.lock')],
                   env=env, check=True, timeout=600)
    subprocess.run(base + ['--no-deps', '--no-build-isolation', str(ROOT)],
                   env=env, check=True, timeout=300)
    subprocess.run([str(binary), '-m', 'pip', 'check'], env=env, check=True, timeout=60)
    subprocess.run([str(binary), str(Path(__file__).resolve()), 'verify', '--locks', str(locks)],
                   env=env, check=True, timeout=60)
    if github_path:
        if os.environ.get('GITHUB_ACTIONS') != 'true' or 'GITHUB_PATH' not in os.environ:
            raise ValueError('--github-path requires a GitHub Actions runner')
        with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as stream:
            stream.write(str(binary.parent) + '\n')
    print(f'Locked build interpreter: {binary}')


def verify(locks):
    metadata = validate(locks / target())
    expected = {entry['name']: entry['version'] for entry in metadata['distributions']}
    actual = {canonical(d.metadata['Name']): d.version for d in importlib.metadata.distributions()}
    expected['gc-controller'] = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    if actual != expected:
        missing = {k: v for k, v in expected.items() if actual.get(k) != v}
        extra = set(actual) - set(expected)
        raise ValueError(f'Installed environment differs from lock: missing/mismatched={missing}, extra={extra}')
    print(f'Verified {len(actual)} installed distributions against the native lock.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['generate', 'install', 'verify'])
    parser.add_argument('--locks', type=Path, default=ROOT / 'requirements' / 'locks')
    parser.add_argument('--venv', type=Path, default=ROOT / '.build-venv')
    parser.add_argument('--github-path', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'generate': generate(args.locks.resolve())
        elif args.action == 'verify': verify(args.locks.resolve())
        else: install(args.locks.resolve(), args.venv.resolve(), args.github_path)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Dependency lock failure: {exc}\n')


if __name__ == '__main__':
    main()
