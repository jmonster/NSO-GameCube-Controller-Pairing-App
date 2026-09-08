import base64
import json
import types
import unittest
from unittest.mock import Mock
from _support import load_definitions


class ParentCommandTests(unittest.TestCase):
    def test_commands_only_use_transport_owned_by_current_process(self):
        method = load_definitions('app.py', {'_send_ble_cmd'},
                                  class_name='GCControllerEnabler')['_send_ble_cmd']
        proc = types.SimpleNamespace(stdin=Mock())
        transport = Mock(process=proc)
        app = types.SimpleNamespace(_ble_subprocess=proc, _ble_commands=transport)
        method(app, {'cmd': 'open'})
        transport.send.assert_called_once_with({'cmd': 'open'})
        proc.stdin.write.assert_not_called()
        app._ble_subprocess = object()
        self.assertFalse(method(app, {'cmd': 'open'}))
        transport.send.assert_called_once()

    def test_rumble_includes_controller_address_when_ui_slot_was_reassigned(self):
        method = load_definitions('app.py', {'_set_rumble_hardware'},
            {'base64': base64, 'build_rumble_packet': lambda on, tid: b'xyz'},
            class_name='GCControllerEnabler')['_set_rumble_hardware']
        slot = types.SimpleNamespace(rumble_state=False, rumble_tid=0, ble_connected=True,
                                     ble_address='correct-controller')
        app = types.SimpleNamespace(slots=[None, slot], _send_ble_cmd=Mock())
        method(app, 1, True)
        self.assertEqual(app._send_ble_cmd.call_args.args[0]['address'], 'correct-controller')
