import sys

# Run packaging diagnostics before importing UI code or starting BLE helpers.
if len(sys.argv) > 1 and sys.argv[1] == '--bundle-self-test':
    from gc_controller.bundle_diagnostics import main as bundle_main
    raise SystemExit(bundle_main(sys.argv[2:]))

# When running as a PyInstaller frozen binary, the exe re-invokes itself
# with a subprocess flag for BLE child processes.  Dispatch here before
# importing the full app (avoids loading Tkinter / heavy deps in children).
if len(sys.argv) > 1 and sys.argv[1] == '--ble-subprocess':
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip flag
    from gc_controller.ble.ble_subprocess import main as ble_main
    ble_main()
elif len(sys.argv) > 1 and sys.argv[1] == '--bleak-subprocess':
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip flag
    from gc_controller.ble.bleak_subprocess import main as bleak_main
    bleak_main()
else:
    from gc_controller.app import main
    main()
