# Windows path-separator regression

The initial stabilization matrix exposed four failing Dolphin directory tests on
Windows. They all came from two calls that passed slash-delimited directory
fragments to `os.path.join`. Mocking `sys.platform` selects a macOS/Linux branch,
but intentionally does not replace the host's filesystem implementation.

The resolver now joins each component separately (`Library`, `Application
Support`, `Dolphin`; `.local`, `share`). Real macOS/Linux destinations are
unchanged, while Windows receives consistent native separators. The existing
assertions remain intact; no tests were skipped or normalized to hide the bug.

`DolphinPathSemanticsTests` additionally runs the production resolver against
`ntpath` on every host. It reproduces the original separator mismatch even on
Linux/macOS and covers both missing and empty `XDG_DATA_HOME`.

```sh
python -m unittest discover -s tests -p test_dolphin_paths.py -v
python -m unittest discover -s tests -v
```

This is a path-construction regression test, not a claim that Windows supports
Dolphin's POSIX FIFO backend. FIFO and privilege-dependent symlink tests retain
their existing platform guards.
