import sys

from gc_controller.process_io import prepare_standard_streams

prepare_standard_streams(ipc=len(sys.argv) > 1 and sys.argv[1] in (
    "--ble-subprocess", "--bleak-subprocess", "--package-smoke"))

# When running as a PyInstaller frozen binary, the exe re-invokes itself
# with a subprocess flag for BLE child processes.  Dispatch here before
# importing the full app (avoids loading Tkinter / heavy deps in children).
if len(sys.argv) > 1 and sys.argv[1] == '--ble-subprocess':
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip flag
    from gc_controller.ble.ble_subprocess import main as ble_main
    sys.exit(ble_main())
elif len(sys.argv) > 1 and sys.argv[1] == '--bleak-subprocess':
    sys.argv = [sys.argv[0]] + sys.argv[2:]  # strip flag
    from gc_controller.ble.bleak_subprocess import main as bleak_main
    sys.exit(bleak_main())
elif len(sys.argv) > 1 and sys.argv[1] == "--package-smoke":
    from gc_controller.package_smoke import main as smoke_main
    sys.exit(smoke_main())
else:
    from gc_controller.app import main
    main()
