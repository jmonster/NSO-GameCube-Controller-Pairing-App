"""No network or pip install: exercise locked-input and release verification."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

lock = load_tool('dependency_lock')
with patch.dict(sys.modules, {'dependency_lock': lock}):
    artifact = load_tool('artifact_manifest')


class LockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copy(ROOT / 'pyproject.toml', self.root)
        self.directory = self.root / 'locks'
        shutil.copytree(ROOT / 'requirements/locks/cp312-darwin-arm64', self.directory)
        self.target = 'cp312-darwin-arm64'

    def validate(self):
        return lock.validate(self.directory, root=self.root, expected_target=self.target)

    def change(self, fn):
        path = self.directory / 'manifest.json'; data = json.loads(path.read_text())
        fn(data); path.write_text(json.dumps(data))

    def test_all_committed_native_locks_match_authoritative_inputs(self):
        for p in (ROOT / 'requirements/locks').iterdir():
            with self.subTest(target=p.name): lock.validate(p, expected_target=p.name)

    def test_missing_or_modified_lock_never_has_unlocked_fallback(self):
        (self.directory / 'requirements.lock').write_bytes(b'--extra-index-url https://untrusted.invalid\n')
        with self.assertRaisesRegex(ValueError, 'checksum'): self.validate()
        (self.directory / 'requirements.lock').unlink()
        with self.assertRaises(FileNotFoundError): self.validate()

    def test_changed_build_input_invalidates_old_resolution(self):
        p = self.root / 'pyproject.toml'; p.write_text(p.read_text().replace('hidapi>=0.14.0', 'hidapi>=0.16.0'))
        with self.assertRaisesRegex(ValueError, 'manifest changed'): self.validate()

    def test_wrong_target_and_path_inventory_are_rejected(self):
        self.change(lambda d: d.update(target='cp313-win32-x86_64'))
        with self.assertRaises(ValueError): self.validate()
        self.change(lambda d: d.update(target=self.target, files={'../outside': 'x'}))
        with self.assertRaisesRegex(ValueError, 'inventory'): self.validate()

    def test_same_hashes_cannot_hide_altered_distribution_metadata(self):
        self.change(lambda d: d['distributions'][0].update(requirement='--trusted-host unsafe'))
        with self.assertRaisesRegex(ValueError, 'inventory'): self.validate()

    def test_missing_archive_hash_and_unpinned_direct_vcs_are_rejected(self):
        item = {'metadata': {'name': 'vgamepad', 'version': '0.1.3'}, 'is_direct': True,
                'download_info': {'url': 'git+https://github.com/yannbouteiller/vgamepad.git'}}
        with self.assertRaisesRegex(ValueError, 'missing archive'): lock.entries_from_report({'install': [item]})

    def test_unapproved_download_hosts_credentials_and_option_injection_rejected(self):
        for url in ('http://files.pythonhosted.org/a.whl', 'https://evil.invalid/a.whl',
                    'https://user:pass@files.pythonhosted.org/a.whl'):
            item = {'metadata': {'name': 'pip', 'version': '1'}, 'download_info': {'url': url,
                    'archive_info': {'hashes': {'sha256': 'a'*64}}}}
            with self.subTest(url=url), self.assertRaises(ValueError): lock.entries_from_report({'install': [item]})
        item['download_info']['url'] = 'https://files.pythonhosted.org/a.whl'
        item['metadata']['version'] = '1\n--extra-index-url=https://evil.invalid'
        with self.assertRaises(ValueError): lock.entries_from_report({'install': [item]})

    def test_dirty_build_environment_rejected_without_pip_or_deletion(self):
        parent = self.root / 'native'; shutil.copytree(self.directory, parent / self.target)
        destination = self.root / 'environment'; destination.mkdir(); marker = destination / 'keep'; marker.touch()
        with patch.object(lock, 'target', return_value=self.target), patch.object(lock.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'already exists'): lock.install(parent, destination)
            run.assert_not_called(); self.assertTrue(marker.exists())

    def test_install_uses_hashed_bootstrap_and_no_unlocked_source_build_deps(self):
        parent = self.root / 'native'; shutil.copytree(self.directory, parent / self.target)
        with patch.object(lock, 'target', return_value=self.target), \
             patch.object(lock.venv, 'EnvBuilder') as builder, patch.object(lock.subprocess, 'run') as run:
            lock.install(parent, self.root / 'fresh')
            calls = [c.args[0] for c in run.call_args_list]
            self.assertIn('--require-hashes', calls[0]); self.assertIn('--only-binary=:all:', calls[0])
            self.assertIn('--require-hashes', calls[1]); self.assertIn('--no-build-isolation', calls[1])
            self.assertIn('--no-deps', calls[2]); self.assertIn('--no-build-isolation', calls[2])
            for c in run.call_args_list: self.assertEqual(c.kwargs['env']['VGAMEPAD_SKIP_VIGEMBUS_INSTALL'], 'true')

    def test_hash_inputs_are_eol_stable_on_windows_checkout(self):
        self.assertIn('requirements/locks/** text eol=lf', (ROOT / '.gitattributes').read_text())
        for path in (ROOT / 'requirements/locks').rglob('*'):
            if path.is_file(): self.assertNotIn(b'\r', path.read_bytes())


class ArtifactTests(unittest.TestCase):
    source = '1' * 40

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.target = artifact.TARGETS['macOS']
        metadata, sha = artifact.lock_metadata(self.target)
        self.smoke = {'frozen': True, 'errors': [], 'platform': 'darwin', 'architecture': 'arm64', 'python': '3.12.9',
                      'checked': ['hid','usb.core','customtkinter','tkinter','_tkinter','PIL.Image',
                                  'Tcl resources','Controller resources','gc_controller.usb_worker']}
        self.archive = self.directory / (artifact.PREFIX + 'macOS.zip')
        self.archive.write_bytes(b'test fixture, no extraction')
        self.record_path = self.archive.with_name(self.archive.name + '.provenance.json')
        self.record = {'format': 1, 'source_commit': self.source, 'target': self.target,
            'artifact': {'name': self.archive.name, 'sha256': artifact.checksum(self.archive), 'bytes': self.archive.stat().st_size},
            'lock_manifest_sha256': sha, 'inputs_sha256': metadata['inputs_sha256'],
            'build': {'distribution_versions': {r['name']: r['version'] for r in metadata['distributions']}},
            'frozen_smoke': self.smoke}
        self.save()

    def save(self): self.record_path.write_text(json.dumps(self.record))
    def verify(self): return artifact.verify(self.directory, self.source, ['macOS'])

    def test_matching_artifact_and_committed_inputs_produce_checksum_inventory(self):
        text = self.verify(); self.assertIn(self.archive.name, text); self.assertEqual(len(text.splitlines()), 2)

    def test_corrupted_or_truncated_archive_is_rejected(self):
        self.archive.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'verification failed'): self.verify()

    def test_source_lock_target_or_dependency_substitution_fails(self):
        original = copy.deepcopy(self.record)
        for change in ({'source_commit': '2'*40}, {'target': artifact.TARGETS['Windows']},
                       {'lock_manifest_sha256': '0'*64}, {'inputs_sha256': '0'*64},
                       {'build': {'distribution_versions': {}}}):
            with self.subTest(change=change):
                self.record = {**copy.deepcopy(original), **change}; self.save()
                with self.assertRaises(ValueError): self.verify()

    def test_missing_extra_or_wrong_named_artifact_fails_closed(self):
        extra = self.directory / 'unexpected.zip'; extra.touch()
        with self.assertRaisesRegex(ValueError, 'inventory'): self.verify()
        extra.unlink(); self.record_path.unlink()
        with self.assertRaisesRegex(ValueError, 'inventory'): self.verify()

    def test_manifest_cannot_redirect_archive_to_parent(self):
        self.record['artifact']['name'] = '../outside.zip'; self.save()
        with self.assertRaises(ValueError): self.verify()

    def test_failed_or_wrong_architecture_smoke_cannot_be_published(self):
        for change in ({'errors': ['failure']}, {'frozen': False}, {'architecture': 'x86_64'}, {'checked': []}):
            with self.subTest(change=change):
                self.record['frozen_smoke'] = {**self.smoke, **change}; self.save()
                with self.assertRaises(ValueError): self.verify()

    def test_full_release_requires_all_three_targets(self):
        with self.assertRaisesRegex(ValueError, 'inventory'): artifact.verify(self.directory, self.source)

    def test_invalid_source_sha_cannot_be_used(self):
        for sha in ('main', 'abc', True, '../tag'):
            with self.subTest(sha=sha), self.assertRaises(ValueError): artifact.verify(self.directory, sha)

    @unittest.skipIf(sys.platform == 'win32', 'Symlink creation requires Windows privileges')
    def test_symlink_archive_is_not_accepted(self):
        payload = self.directory.parent / (self.directory.name + '-outside')
        self.addCleanup(payload.unlink)
        self.archive.rename(payload); self.archive.symlink_to(payload)
        with self.assertRaisesRegex(ValueError, 'regular file'): self.verify()


class ElevatedSourceTests(unittest.TestCase):
    def test_linux_launches_do_not_forward_python_search_paths(self):
        import ast
        tree = ast.parse((ROOT / 'src/gc_controller/app.py').read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                 isinstance(n.value, ast.List) and any(isinstance(x, ast.Constant) and x.value == '-I'
                                                      for x in n.value.elts)]
        self.assertEqual(len(calls), 2, 'GUI and headless launchers must use isolated source Python')
        child = (ROOT / 'src/gc_controller/ble/ble_subprocess.py').read_text()
        self.assertNotIn('sys.argv[1].split', child)
        self.assertIn('sys.argv[1:] != expected', child)

    def test_elevated_source_rejects_supplied_import_paths_without_starting_hardware(self):
        import subprocess
        child = ROOT / 'src/gc_controller/ble/ble_subprocess.py'
        result = subprocess.run([sys.executable, '-I', str(child), '/untrusted/import/path'],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b'')


class PackageArchiveTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(sys.modules, {'artifact_manifest': artifact}): self.package = load_tool('package_archive')

    def test_zip_has_expected_bytes_and_unix_executable_mode(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'binary'; source.write_bytes(b'packaged executable')
            if sys.platform != 'win32': source.chmod(0o755)
            with zipfile.ZipFile(root / 'package.zip', 'x') as archive: self.package.add(archive, source, 'app')
            with zipfile.ZipFile(root / 'package.zip') as archive:
                self.assertEqual(archive.read('app'), source.read_bytes())
                if sys.platform != 'win32': self.assertEqual((archive.getinfo('app').external_attr >> 16) & 0o777, 0o755)

    @unittest.skipIf(sys.platform == 'win32', 'Symlink creation requires Windows privileges')
    def test_app_symlink_is_preserved_instead_of_followed(self):
        import zipfile
        import stat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'file').write_bytes(b'target')
            link = root / 'link'; link.symlink_to('file')
            with zipfile.ZipFile(root / 'package.zip', 'x') as archive: self.package.add(archive, link, 'App.app/link')
            with zipfile.ZipFile(root / 'package.zip') as archive:
                self.assertEqual(archive.read('App.app/link'), b'file')
                self.assertTrue(stat.S_ISLNK(archive.getinfo('App.app/link').external_attr >> 16))
