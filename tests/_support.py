"""Load production modules with explicitly faked hardware/GUI imports.

These tests do not import or initialize any real hardware backend. Loading
under a private package name also keeps fakes out of other tests' imports.
"""
import ast
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src' / 'gc_controller'


def fake_module(**attributes):
    module = types.ModuleType('test_fake')
    module.__dict__.update(attributes)
    return module


def load_module(relative_path, fakes=None):
    """Load one source module; fakes are relative module names -> modules."""
    package_name = '_gc_stability_test'
    package = types.ModuleType(package_name)
    package.__path__ = [str(SRC)]
    modules = {package_name: package}
    name = package_name + '.' + relative_path.removesuffix('.py').replace('/', '.')
    for fake_name, fake in (fakes or {}).items():
        modules[package_name + '.' + fake_name] = fake
    # Avoid running ble/__init__.py (which probes optional native libraries).
    if relative_path.startswith('ble/'):
        ble = types.ModuleType(package_name + '.ble')
        ble.__path__ = [str(SRC / 'ble')]
        modules[ble.__name__] = ble
    spec = importlib.util.spec_from_file_location(name, SRC / relative_path)
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def load_definitions(relative_path, names, namespace=None, class_name=None):
    """Compile selected production definitions without module startup effects.

Used only for GUI entry points and build-spec literals, not for the input
or connection engines. Bodies are read directly from production source.
"""
    tree = ast.parse((SRC / relative_path).read_text(encoding='utf-8'))
    body = tree.body
    if class_name:
        body = next(n.body for n in body
                    if isinstance(n, ast.ClassDef) and n.name == class_name)
    nodes = [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise AssertionError('Requested production definitions not found')
    result = {} if namespace is None else dict(namespace)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative_path, 'exec'), result)
    return result
