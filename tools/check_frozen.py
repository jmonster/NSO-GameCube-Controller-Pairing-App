"""Run a packaged executable outside its source/build tree, with no hardware."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    executable = str(Path(sys.argv[1]).resolve(strict=True))
    report_path = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    with tempfile.TemporaryDirectory(prefix='gc-package-smoke-') as cwd:
        env = {k: v for k, v in os.environ.items() if k not in (
            'PYTHONPATH', 'PYTHONHOME', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH',
            'DYLD_FALLBACK_LIBRARY_PATH', 'DOLPHIN_EMU_USERPATH')}
        env.update(HOME=cwd, USERPROFILE=cwd, XDG_CONFIG_HOME=cwd, APPDATA=cwd)
        # Popen.communicate concurrently drains output; timeout reaps the child.
        result = subprocess.run([executable, '--package-smoke'], input=bytes(range(256)),
                                capture_output=True, cwd=cwd, env=env, timeout=90)
        header, separator, binary = result.stdout.partition(b'\n')
        if not separator:
            raise RuntimeError(f'No smoke response: {result.stderr.decode(errors="replace")}')
        report = json.loads(header)
        print(json.dumps(report, indent=2))
        if report_path is not None:
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        if result.returncode or report['errors'] or not report['frozen'] or binary != bytes(range(256)):
            raise RuntimeError(f'Frozen dependency/pipe check failed: {result.stderr.decode(errors="replace")}')
        if sys.platform in ('darwin', 'win32'):
            # The real Bleak child imports its platform backend and runs the
            # actual IPC command lifecycle. open() does not scan or connect.
            result = subprocess.run([executable, '--bleak-subprocess'],
                input=b'{"cmd":"open"}\n{"cmd":"shutdown"}\n',
                capture_output=True, cwd=cwd, env=env, timeout=45)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            if result.returncode or {'e': 'ready', 'protocol': 2} not in events or {'e': 'open_ok'} not in events or any(e['e'] == 'error' for e in events):
                raise RuntimeError(f'Frozen BLE child check failed: {events!r}; {result.stderr.decode(errors="replace")}')
            print('Frozen BLE child: ready, open, shutdown, EOF; no Bluetooth scan performed')


if __name__ == '__main__':
    main()
