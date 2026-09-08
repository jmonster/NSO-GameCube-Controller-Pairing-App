"""
Emulation Manager

Handles virtual controller creation, teardown, and the hot-path
update that maps GC input to the virtual gamepad.

Supports Xbox 360 mode and Dolphin named pipe mode.
"""

import errno
import logging
import threading
from typing import Optional, Dict

from .virtual_gamepad import VirtualGamepad, create_gamepad
from .controller_constants import BUTTON_MAPPING, DOLPHIN_BUTTON_MAPPING
from .calibration import CalibrationManager

logger = logging.getLogger(__name__)


class EmulationManager:
    """Manages controller emulation lifecycle and input forwarding."""

    def __init__(self, cal_mgr: CalibrationManager):
        self._cal_mgr = cal_mgr
        self.gamepad: Optional[VirtualGamepad] = None
        self.is_emulating = False
        self.mode: str = 'xbox360'
        self._prev_buttons: Dict[str, bool] = {}
        self._output_lock = threading.RLock()
        self._pending = None
        self._generation = 0

    def start(self, mode: str = 'xbox360', slot_index: int = 0,
              cancel_event: threading.Event | None = None,
              rumble_callback=None) -> None:
        """Create the virtual gamepad and begin emulation. Raises on failure."""
        cancel = cancel_event if cancel_event is not None else threading.Event()
        with self._output_lock:
            if cancel.is_set():
                raise OSError(errno.ECANCELED, 'Emulation start was cancelled')
            if self.gamepad is not None or self._pending is not None:
                raise OSError(errno.EBUSY, 'Emulation is active or still finishing a start')
            self._generation += 1
            generation = self._generation
            self._pending = cancel

        # Never hold the output lock across a blocking factory. Stop must be
        # able to cancel it even before Dolphin opens the FIFO reader.
        pad = None
        try:
            logger.info("Starting emulation: mode=%s slot=%d", mode, slot_index)
            pad = create_gamepad(mode, slot_index=slot_index, cancel_event=cancel)
            if rumble_callback and mode in ('xbox360', 'dsu'):
                def owned_rumble(*args, _pad=pad):
                    if (self.gamepad is _pad and self.is_emulating and
                            self._generation == generation):
                        rumble_callback(*args)
                pad.set_rumble_callback(owned_rumble)
            with self._output_lock:
                if cancel.is_set() or self._generation != generation:
                    raise OSError(errno.ECANCELED, 'Emulation start was cancelled')
                self.mode = mode
                self._prev_buttons = {}
                self.gamepad = pad
                self.is_emulating = True
                pad = None  # Ownership transferred; finally must not close it.
        finally:
            # A failed/cancelled factory result must never leak a virtual device.
            # Keep the pending reservation until disposal has finished so a new
            # factory cannot reuse its slot while the old result is closing.
            try:
                if pad is not None:
                    self._dispose(pad)
            finally:
                with self._output_lock:
                    if self._pending is cancel:
                        self._pending = None

    @property
    def is_starting(self) -> bool:
        with self._output_lock:
            return self._pending is not None

    @staticmethod
    def _dispose(pad):
        for operation in (pad.stop_rumble_listener, pad.reset, pad.update, pad.close):
            try:
                operation()
            except Exception:
                logger.debug("Output teardown operation failed", exc_info=True)

    def stop(self) -> None:
        """Cancel creation, then serialize neutralization against input updates."""
        with self._output_lock:
            self._generation += 1
            if self._pending is not None:
                self._pending.set()
            logger.info("Stopping emulation (mode=%s)", self.mode)
            self.is_emulating = False
            self._prev_buttons = {}
            pad, self.gamepad = self.gamepad, None
            if pad is not None:
                self._dispose(pad)

    def update(self, left_x, left_y, right_x, right_y,
               left_trigger, right_trigger, button_states: Dict[str, bool]):
        """Update virtual Xbox 360 controller state (hot path)."""
        with self._output_lock:
            if not self.gamepad:
                return

            try:
                stick_scale = 32767
                left_x_scaled = int(max(-32767, min(32767, left_x * stick_scale)))
                left_y_scaled = int(max(-32767, min(32767, left_y * stick_scale)))
                right_x_scaled = int(max(-32767, min(32767, right_x * stick_scale)))
                right_y_scaled = int(max(-32767, min(32767, right_y * stick_scale)))

                self.gamepad.left_joystick(x_value=left_x_scaled, y_value=left_y_scaled)
                self.gamepad.right_joystick(x_value=right_x_scaled, y_value=right_y_scaled)

                # Process analog triggers with calibration
                left_trigger_calibrated = self._cal_mgr.calibrate_trigger_fast(left_trigger, 'left')
                right_trigger_calibrated = self._cal_mgr.calibrate_trigger_fast(right_trigger, 'right')

                # Only emit press/release on state changes (delta updates)
                map_home = self._cal_mgr._calibration.get('map_home_to_guide', True)
                mapping = DOLPHIN_BUTTON_MAPPING if self.mode == 'dolphin_pipe' else BUTTON_MAPPING
                for button_name, xbox_button in mapping.items():
                    if button_name == 'Home' and not map_home:
                        # Ensure Guide is released if mapping was just disabled
                        if self._prev_buttons.get('Home', False):
                            self.gamepad.release_button(xbox_button)
                            self._prev_buttons['Home'] = False
                        continue
                    pressed = button_states.get(button_name, False)
                    if pressed != self._prev_buttons.get(button_name, False):
                        if pressed:
                            self.gamepad.press_button(xbox_button)
                        else:
                            self.gamepad.release_button(xbox_button)
                        self._prev_buttons[button_name] = pressed

                # Handle shoulder buttons and triggers
                l_pressed = button_states.get('L', False)
                r_pressed = button_states.get('R', False)

                if l_pressed and self.mode != 'dolphin_pipe':
                    self.gamepad.left_trigger(255)
                else:
                    self.gamepad.left_trigger(left_trigger_calibrated)

                if r_pressed and self.mode != 'dolphin_pipe':
                    self.gamepad.right_trigger(255)
                else:
                    self.gamepad.right_trigger(right_trigger_calibrated)

                self.gamepad.update()

            except Exception as e:
                print(f"Virtual controller update error: {e}")


class OutputStart:
    """Cancellable output creation for event-loop consumers such as headless BLE.

    Publish this object to its owning slot before calling launch(). stop() may
    then cancel a worker that has not started yet as well as a running factory.
    on_complete receives the operation itself for stale-completion checks.
    """
    def __init__(self, manager, mode, slot_index, rumble_callback, on_complete):
        self.manager = manager
        self.mode = mode
        self.slot_index = slot_index
        self.rumble_callback = rumble_callback
        self.on_complete = on_complete
        self.cancel_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='controller-output-start', daemon=True)

    def launch(self):
        self.thread.start()

    def stop(self):
        self.cancel_event.set()
        self.manager.stop()

    def _run(self):
        error = None
        try:
            self.manager.start(self.mode, slot_index=self.slot_index,
                               cancel_event=self.cancel_event, rumble_callback=self.rumble_callback)
        except Exception as exc:
            error = str(exc)
        try:
            self.on_complete(self, error)
        except Exception:
            logger.exception('Output completion callback failed')
