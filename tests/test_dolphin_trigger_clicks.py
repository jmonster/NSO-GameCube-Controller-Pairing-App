"""Port HotTownJohnny's digital triggers through the complete input/output path."""
import io
import types
import unittest
from unittest.mock import Mock
from _support import fake_module, load_module


class DolphinTriggerTests(unittest.TestCase):
    def setUp(self):
        self.virtual = load_module('virtual_gamepad.py')
        self.constants = load_module('controller_constants.py', {'virtual_gamepad': self.virtual})
        module = load_module('emulation_manager.py', {
            'virtual_gamepad': self.virtual, 'controller_constants': self.constants,
            'calibration': fake_module(CalibrationManager=object),
        })
        cal = types.SimpleNamespace(_calibration={}, calibrate_trigger_fast=lambda value, side: value)
        self.emu = module.EmulationManager(cal)
        self.emu.mode = 'dolphin_pipe'
        self.pad = self.virtual.DolphinPipeGamepad.__new__(self.virtual.DolphinPipeGamepad)
        self.pad._pipe = io.StringIO()
        self.pad._pressed = set()
        self.emu.gamepad = self.pad

    def update(self, states, left=64, right=128):
        self.emu.update(0, 0, 0, 0, left, right, states)

    def test_click_edges_reach_pipe_independently_of_analog_travel(self):
        self.update({'L': True, 'R': True})
        self.update({'L': True, 'R': True})
        self.update({'L': False, 'R': False})
        text = self.pad._pipe.getvalue()
        for side in ('L', 'R'):
            self.assertEqual(text.count('PRESS ' + side + '\n'), 1)
            self.assertEqual(text.count('RELEASE ' + side + '\n'), 1)
        self.assertIn('SET L 0.2510\n', text)
        self.assertIn('SET R 0.5020\n', text)
        self.assertNotIn('SET L 1.0000', text)

    def test_zl_cannot_press_or_release_physical_l_click(self):
        self.update({'ZL': True})
        self.update({'L': True, 'ZL': True})
        self.update({'L': True, 'ZL': False})
        text = self.pad._pipe.getvalue()
        self.assertEqual(text.count('PRESS L\n'), 1)
        self.assertNotIn('RELEASE L\n', text)
        self.assertNotIn('PRESS ZL\n', text)  # Dolphin has no such token.

    def test_xbox_mapping_and_trigger_promotion_are_unchanged(self):
        self.emu.mode = 'xbox360'
        pad = self.emu.gamepad = Mock()
        self.update({'L': True, 'R': True, 'ZL': True})
        pad.left_trigger.assert_called_once_with(255)
        pad.right_trigger.assert_called_once_with(255)
        pad.press_button.assert_called_once_with(self.virtual.GamepadButton.LEFT_SHOULDER)

    def test_reset_releases_both_trigger_clicks(self):
        self.update({'L': True, 'R': True})
        self.pad.reset()
        text = self.pad._pipe.getvalue()
        self.assertIn('RELEASE L\n', text)
        self.assertIn('RELEASE R\n', text)
        self.assertEqual(self.pad._pressed, set())
