import base64
import json
import types
import unittest
from unittest.mock import Mock
from _support import load_definitions


class ParentCommandTests(unittest.TestCase):
    def test_write_failure_is_delivered_with_original_process_owner(self):
        method = load_definitions('app.py', {'_send_ble_cmd'}, {'json': json},
                                  class_name='GCControllerEnabler')['_send_ble_cmd']
        proc = types.SimpleNamespace(poll=lambda: None, stdin=Mock())
        proc.stdin.write.side_effect = BrokenPipeError('closed')
        app = types.SimpleNamespace(_ble_subprocess=proc, _call_on_ui_thread=Mock(),
                                    _ble_service_lost=Mock())
        method(app, {'cmd': 'open'})
        args = app._call_on_ui_thread.call_args.args
        self.assertIs(args[1], proc)
        self.assertIn('closed', args[2])
        app._ble_service_lost.assert_not_called()

    def test_rumble_includes_controller_address_when_ui_slot_was_reassigned(self):
        method = load_definitions('app.py', {'_set_rumble_hardware'},
            {'base64': base64, 'build_rumble_packet': lambda on, tid: b'xyz'},
            class_name='GCControllerEnabler')['_set_rumble_hardware']
        slot = types.SimpleNamespace(rumble_state=False, rumble_tid=0, ble_connected=True,
                                     ble_address='correct-controller')
        app = types.SimpleNamespace(slots=[None, slot], _send_ble_cmd=Mock())
        method(app, 1, True)
        self.assertEqual(app._send_ble_cmd.call_args.args[0]['address'], 'correct-controller')
