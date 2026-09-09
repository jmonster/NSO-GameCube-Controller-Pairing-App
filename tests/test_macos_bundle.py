import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "src/gc_controller/bundle_diagnostics.py"
spec = importlib.util.spec_from_file_location("bundle_diagnostics", MODULE)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class BundleChecks(unittest.TestCase):
    def test_source_tree_is_not_reported_as_a_working_bundle(self):
        with patch.object(diag.sys, "frozen", False, create=True):
            result = diag.inspect_bundle()
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"], [])

    def test_bundled_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = SimpleNamespace(__file__=str(Path(tmp) / "bleak/__init__.pyc"))
            with patch.object(diag.importlib, "import_module", return_value=module):
                self.assertTrue(diag.check_module("bleak", Path(tmp))["ok"])

    def test_external_import_is_rejected(self):
        module = SimpleNamespace(__file__="/opt/homebrew/lib/bleak/__init__.py")
        with patch.object(diag.importlib, "import_module", return_value=module):
            self.assertFalse(diag.check_module("bleak", Path("/Applications/Test.app"))["ok"])

    def test_missing_import_is_reported(self):
        with patch.object(diag.importlib, "import_module", side_effect=ImportError("missing")):
            result = diag.check_module("CoreBluetooth", Path("/unused"))
        self.assertFalse(result["ok"])
        self.assertIn("ImportError", result["error"])

    def test_import_without_origin_is_rejected(self):
        with patch.object(diag.importlib, "import_module", return_value=SimpleNamespace()):
            self.assertFalse(diag.check_module("bleak", Path("/unused"))["ok"])

    def test_missing_libusb_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                diag.find_bundled_libusb(Path(tmp), Path(tmp))

    def test_bundled_libusb_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = root / "libusb-1.0.dylib"
            lib.touch()
            self.assertEqual(diag.find_bundled_libusb(root, root), lib.resolve())

    def test_external_libusb_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "app"
            bundle.mkdir()
            lib = root / "external.dylib"
            lib.touch()
            try:
                (bundle / "libusb-1.0.dylib").symlink_to(lib)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaises(RuntimeError):
                diag.find_bundled_libusb(bundle, bundle)

    def test_report_file_works_without_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            with patch.object(diag, "inspect_bundle", return_value={"ok": False}), \
                 patch.object(diag.sys, "stdout", None):
                self.assertEqual(diag.main(["--report", str(report)]), 1)
            self.assertEqual(json.loads(report.read_text()), {"ok": False})


if __name__ == "__main__":
    unittest.main()
