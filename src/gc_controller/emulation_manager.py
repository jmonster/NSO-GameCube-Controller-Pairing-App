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

    def start(self, mode: str = 'xbox360', slot_index: int = 0,
              cancel_event: threading.Event | None = None,
              rumble_callback=None) -> None:
        """Create the virtual gamepad and begin emulation. Raises on failure."""
        self.mode = mode
        self._prev_buttons = {}
        logger.info("Starting emulation: mode=%s slot=%d", mode, slot_index)
        self.gamepad = create_gamepad(mode, slot_index=slot_index,
                                     cancel_event=cancel_event)
        if rumble_callback and mode in ('xbox360', 'dsu'):
            self.gamepad.set_rumble_callback(rumble_callback)
        self.is_emulating = True

    def stop(self) -> None:
        """Serialize teardown with updates and send a neutral state before close."""
        with self._output_lock:
            logger.info("Stopping emulation (mode=%s)", self.mode)
            self.is_emulating = False
            self._prev_buttons = {}
            pad, self.gamepad = self.gamepad, None
            if pad is None:
                return
            for operation in (pad.stop_rumble_listener, pad.reset, pad.update, pad.close):
                try:
                    operation()
                except Exception:
                    logger.debug("Output teardown operation failed", exc_info=True)

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
