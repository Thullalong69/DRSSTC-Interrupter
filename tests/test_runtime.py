import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from credentials import create_credentials, load_credentials
from process_lock import ProcessLock
from run import main as run_server
from test_power import FakeGPIO, main


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        main.pi = None
        main.runtime_owner = None
        main.runtime_thread = None
        main.runtime_ready = False
        main.output_controller = main.create_output_controller()
        main.control_lease = main.ControlLease(lambda reason: main.safe_power_off(reason))
        main.power_cancel = None
        main.softstart_active = False

    def tearDown(self):
        if main.runtime_owner is not None:
            main.shutdown_hardware()
        self.directory.cleanup()

    def test_import_never_connects_gpio_or_starts_watchdog(self):
        with patch('pigpio.pi') as connect, patch('threading.Thread.start') as start:
            spec = importlib.util.spec_from_file_location('safe_import_test', main.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        connect.assert_not_called()
        start.assert_not_called()
        self.assertIsNone(module.pi)
        self.assertFalse(module.runtime_ready)

    def test_startup_sets_outputs_off_before_ready_indicator(self):
        gpio = FakeGPIO()
        with patch('pigpio.pi', return_value=gpio), patch('threading.Thread.start'):
            main.initialize_hardware(self.root / 'lock')
        self.assertEqual(gpio.calls[:3], [(main.SOFTSTART_PIN, 1), (main.FULLPOWER_PIN, 1), (main.INTERRUPTER_PIN, 0)])
        self.assertEqual(gpio.calls[-1], (main.READY_LED_PIN, 1))
        self.assertTrue(main.runtime_ready)
        self.assertNotIn((main.SOFTSTART_PIN, 0), gpio.calls)
        self.assertNotIn((main.FULLPOWER_PIN, 0), gpio.calls)

    def test_disconnected_daemon_fails_before_any_output_write(self):
        gpio = FakeGPIO()
        gpio.connected = False
        with patch('pigpio.pi', return_value=gpio), patch('threading.Thread.start'):
            with self.assertRaises(RuntimeError):
                main.initialize_hardware(self.root / 'lock')
        # Cleanup can attempt OFF writes, but must never indicate ready or power on.
        self.assertNotIn((main.READY_LED_PIN, 1), gpio.calls)
        self.assertNotIn((main.SOFTSTART_PIN, 0), gpio.calls)
        self.assertFalse(main.runtime_ready)
        self.assertIsNone(main.runtime_owner)

    def test_second_process_is_rejected_before_gpio_connection(self):
        lock = ProcessLock(self.root / 'lock')
        lock.acquire()
        try:
            with patch('pigpio.pi') as connect:
                with self.assertRaises(RuntimeError):
                    main.initialize_hardware(self.root / 'lock')
            connect.assert_not_called()
            code = "from process_lock import ProcessLock\nimport sys\ntry:\n ProcessLock(sys.argv[1]).acquire()\nexcept RuntimeError:\n sys.exit(0)\nsys.exit(3)\n"
            result = subprocess.run([sys.executable, '-c', code, str(self.root / 'lock')], cwd=Path(main.__file__).parent,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
        finally:
            lock.release()

    def test_shutdown_stops_gpio_and_releases_process_lock(self):
        gpio = FakeGPIO()
        with patch('pigpio.pi', return_value=gpio), patch('threading.Thread.start'):
            main.initialize_hardware(self.root / 'lock')
        with patch.object(gpio, 'stop') as disconnect:
            main.shutdown_hardware()
        disconnect.assert_called_once()
        self.assertEqual(gpio.read(main.READY_LED_PIN), 0)
        self.assertEqual(gpio.read(main.SOFTSTART_PIN), 1)
        self.assertEqual(gpio.read(main.FULLPOWER_PIN), 1)
        self.assertIsNone(main.pi)
        self.assertFalse(main.runtime_ready)
        lock = ProcessLock(self.root / 'lock')
        lock.acquire()
        lock.release()

    def test_watchdog_loop_checks_lease_without_pinging(self):
        with patch.object(main.runtime_stop, 'wait', side_effect=[False, True]), patch.object(main.control_lease, 'check_expired') as check:
            main.watchdog()
        check.assert_called_once()

    def test_background_midi_fault_powers_off_without_lock_inversion(self):
        main.pi = FakeGPIO()
        main.pi.levels[main.FULLPOWER_PIN] = 0
        main.control_lease = main.ControlLease(lambda reason: main.safe_power_off(reason, preserve_fault=True))
        with main.control_lease.command('test-operator'):
            token = main.output_controller.begin_midi()
        def fail_active(pin, frequency, duty):
            if frequency:
                raise RuntimeError('background PWM failed')
            return 0
        with patch.object(main.pi, 'hardware_PWM', fail_active), self.assertLogs(main.logger, level='ERROR'):
            with self.assertRaises(main.OutputHardwareError):
                main.output_controller.midi_pwm(token, main.calculate_pwm(130, 50))
        with patch.object(main.runtime_stop, 'wait', side_effect=[False, True]):
            main.watchdog()
        self.assertEqual(main.pi.read(main.FULLPOWER_PIN), 1)
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)
        self.assertIsNone(main.control_lease.owner)
        self.assertEqual(main.output_controller.status()['mode'], 'fault')

    def test_credentials_are_private_and_cannot_be_overwritten(self):
        path = self.root / 'auth.json'
        create_credentials(path, 'test-only-long-password')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('password_hash', load_credentials(path))
        with self.assertRaises(FileExistsError):
            create_credentials(path, 'another-test-password')
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            load_credentials(path)

    def fake_application(self):
        return SimpleNamespace(app=SimpleNamespace(config={}), configure_auth=Mock(),
                               initialize_hardware=Mock(), shutdown_hardware=Mock())

    def test_production_entrypoint_uses_one_server_without_reloader(self):
        application = self.fake_application()
        server = Mock()
        factory = Mock(return_value=server)
        run_server(application, factory, ['--public-origin', 'https://coil.example'])
        self.assertEqual(factory.call_args.kwargs['threads'], 4)
        self.assertNotIn('workers', factory.call_args.kwargs)
        application.initialize_hardware.assert_called_once()
        application.shutdown_hardware.assert_called_once()
        self.assertTrue(application.app.config['SESSION_COOKIE_SECURE'])

    def test_configuration_failure_precedes_hardware_and_server(self):
        application = self.fake_application()
        application.configure_auth.side_effect = ValueError('invalid credentials')
        factory = Mock()
        with self.assertRaises(ValueError):
            run_server(application, factory, ['--public-origin', 'https://coil.example'])
        application.initialize_hardware.assert_not_called()
        factory.assert_not_called()

    def test_signal_and_server_failure_both_run_hardware_cleanup(self):
        for behavior in ('signal', 'error'):
            with self.subTest(behavior=behavior):
                application = self.fake_application()
                server = Mock()
                if behavior == 'signal':
                    server.run.side_effect = lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    expected = SystemExit
                else:
                    server.run.side_effect = RuntimeError('server failed')
                    expected = RuntimeError
                old_handler = signal.getsignal(signal.SIGTERM)
                with self.assertRaises(expected):
                    run_server(application, Mock(return_value=server), ['--allow-local-http'])
                application.shutdown_hardware.assert_called_once()
                self.assertIs(signal.getsignal(signal.SIGTERM), old_handler)

    def test_nonloopback_or_mixed_tls_http_config_fails_before_hardware(self):
        for argv in (['--host', '0.0.0.0', '--allow-local-http'],
                     ['--public-origin', 'https://coil.example', '--allow-local-http']):
            application = self.fake_application()
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                run_server(application, Mock(), argv)
            application.initialize_hardware.assert_not_called()

    def test_shutdown_drains_workers_before_closing_trigger(self):
        order = []
        application = self.fake_application()
        application.shutdown_hardware.side_effect = lambda: order.append('hardware_off')
        server = Mock()
        server.task_dispatcher.shutdown.side_effect = lambda **kwargs: order.append('workers_drained')
        server.close.side_effect = lambda: order.append('server_closed')
        run_server(application, Mock(return_value=server), ['--allow-local-http'])
        self.assertEqual(order, ['hardware_off', 'workers_drained', 'server_closed'])


if __name__ == '__main__':
    unittest.main()
