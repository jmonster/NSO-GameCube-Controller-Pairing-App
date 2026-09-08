"""USB control-transfer ownership with no libusb or controller dependency."""
from unittest import TestCase
from unittest.mock import Mock, patch
import sys

from _support import fake_module, load_module


def load_connection_manager():
    core = fake_module(USBError=OSError, find=Mock(return_value=None))
    util = fake_module(claim_interface=Mock(), release_interface=Mock(),
                       dispose_resources=Mock())
    control = fake_module(get_configuration=Mock(return_value=1))
    usb = fake_module(core=core, util=util, control=control)
    hid = fake_module(device=Mock(), enumerate=Mock(return_value=[]))
    constants = fake_module(VENDOR_ID=0x057e, PRODUCT_ID=0x2073,
                            DEFAULT_REPORT_DATA=b'init',
                            SET_LED_DATA=bytes(12))
    with patch.dict(sys.modules, {'usb': usb, 'usb.core': core,
                                 'usb.util': util, 'usb.control': control,
                                 'hid': hid}):
        module = load_module('connection_manager.py',
                             {'controller_constants': constants})
    module.IS_MACOS = False
    return module


class UsbResourceTests(TestCase):
    def setUp(self):
        self.module = load_connection_manager()
        self.status = Mock()
        self.manager = self.module.ConnectionManager(self.status, Mock())
        self.dev = Mock()
        self.dev.is_kernel_driver_active.return_value = False
        self.usb = self.module.usb

    def test_does_not_reset_existing_configuration(self):
        self.assertTrue(self.manager.initialize_via_usb(self.dev))
        self.dev.set_configuration.assert_not_called()
        self.usb.util.release_interface.assert_called_once_with(self.dev, 1)
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_configures_unconfigured_device(self):
        self.usb.control.get_configuration.return_value = 0
        self.assertTrue(self.manager.initialize_via_usb(self.dev))
        self.dev.set_configuration.assert_called_once_with()

    def test_configuration_query_failure_does_not_write(self):
        self.usb.control.get_configuration.side_effect = OSError('permission denied')
        self.assertFalse(self.manager.initialize_via_usb(self.dev))
        self.dev.write.assert_not_called()
        self.usb.util.claim_interface.assert_not_called()
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)
        self.assertIn('permission denied', self.status.call_args.args[0])

    def test_claim_failure_does_not_write_or_release_unowned_interface(self):
        self.usb.util.claim_interface.side_effect = OSError('busy')
        self.assertFalse(self.manager.initialize_via_usb(self.dev))
        self.dev.write.assert_not_called()
        self.usb.util.release_interface.assert_not_called()
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_configuration_failure_disposes_device(self):
        self.usb.control.get_configuration.return_value = 0
        self.dev.set_configuration.side_effect = OSError('configuration rejected')
        self.assertFalse(self.manager.initialize_via_usb(self.dev))
        self.dev.write.assert_not_called()
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_init_write_failure_releases_and_disposes(self):
        self.dev.write.side_effect = OSError('disconnected')
        self.assertFalse(self.manager.initialize_via_usb(self.dev))
        self.usb.util.release_interface.assert_called_once_with(self.dev, 1)
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_led_write_failure_releases_and_disposes(self):
        self.dev.write.side_effect = OSError('disconnected')
        self.assertFalse(self.manager.set_player_led_usb(self.dev, 2))
        self.usb.util.release_interface.assert_called_once_with(self.dev, 1)
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_release_failure_does_not_prevent_disposal(self):
        self.usb.util.release_interface.side_effect = OSError('gone')
        self.assertTrue(self.manager.set_player_led_usb(self.dev, 2))
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_only_reattaches_a_driver_detached_by_this_operation(self):
        self.module.IS_MACOS = True
        self.assertTrue(self.manager.set_player_led_usb(self.dev, 1))
        self.dev.detach_kernel_driver.assert_not_called()
        self.dev.attach_kernel_driver.assert_not_called()

    def test_mac_driver_is_restored_after_write_failure(self):
        self.module.IS_MACOS = True
        self.dev.is_kernel_driver_active.return_value = True
        self.dev.write.side_effect = OSError('gone')
        self.assertFalse(self.manager.set_player_led_usb(self.dev, 1))
        self.dev.detach_kernel_driver.assert_called_once_with(1)
        self.dev.attach_kernel_driver.assert_called_once_with(1)
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_mac_driver_query_permission_error_is_not_swallowed(self):
        self.module.IS_MACOS = True
        self.dev.is_kernel_driver_active.side_effect = OSError('not authorized')
        self.assertFalse(self.manager.set_player_led_usb(self.dev, 1))
        self.dev.write.assert_not_called()
        self.usb.util.dispose_resources.assert_called_once_with(self.dev)

    def test_unsupported_driver_query_does_not_block_claim(self):
        self.module.IS_MACOS = True
        self.dev.is_kernel_driver_active.side_effect = NotImplementedError
        self.assertTrue(self.manager.set_player_led_usb(self.dev, 1))
        self.usb.util.claim_interface.assert_called_once_with(self.dev, 1)

    def test_invalid_player_number_does_not_touch_device(self):
        for number in (0, 5, -1):
            self.assertFalse(self.manager.set_player_led_usb(self.dev, number))
        self.usb.util.claim_interface.assert_not_called()
        self.dev.write.assert_not_called()
