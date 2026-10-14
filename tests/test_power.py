"""Regression tests using simulated GPIOs; never connect to pigpiod."""
import importlib.util
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class FakeGPIO:
    connected = True

    def __init__(self):
        self.levels = {20: 1, 21: 1}
        self.calls = []
        self.fail_pin = None

    def write(self, pin, value):
        self.calls.append((pin, value))
        if pin == self.fail_pin:
            raise RuntimeError("simulated GPIO failure")
        self.levels[pin] = value

    def read(self, pin):
        return self.levels.get(pin, 0)

    def __getattr__(self, name):
        return lambda *args: 0


with patch('pigpio.pi', FakeGPIO), patch('threading.Thread.start'), patch('time.sleep'):
    spec = importlib.util.spec_from_file_location('power_test_app', ROOT / 'main.py')
    main = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = main
    spec.loader.exec_module(main)
main.app.config.update(TESTING=True, AUTH_DISABLED=True)


class PowerTests(unittest.TestCase):
    def setUp(self):
        main.pi = FakeGPIO()
        main.output_controller = main.create_output_controller()
        main.power_cancel = None
        main.softstart_active = False
        main.softstart_progress = 0
        main.SOFTSTART_SECONDS = 20
        self.client = main.app.test_client()
        self.workers = []

    def tearDown(self):
        main.pi.fail_pin = None
        main.safe_power_off()
        for worker in self.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_off_interrupts_real_worker_without_waiting_for_charge(self):
        real_thread = threading.Thread
        def tracked_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            self.workers.append(worker)
            return worker
        with patch.object(main.threading, 'Thread', tracked_thread):
            response = self.client.post('/toggle_power', json={'power': True})
        self.assertEqual(response.status_code, 200)
        token = main.power_cancel
        self.assertTrue(main.softstart_active)
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 0)
        response = self.client.post('/toggle_power', json={'power': False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['power'])
        self.assertTrue(token.is_set())
        self.workers[0].join(timeout=1)
        self.assertFalse(self.workers[0].is_alive())
        self.assertNotIn((main.FULLPOWER_PIN, 0), main.pi.calls)

    def test_repeated_start_creates_only_one_worker(self):
        with patch('threading.Thread.start') as start:
            first = self.client.post('/toggle_power', json={'power': True})
            token = main.power_cancel
            second = self.client.post('/start_softstart')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(start.call_count, 1)
        self.assertIs(main.power_cancel, token)

    def test_old_worker_cannot_enable_or_change_new_start(self):
        with patch('threading.Thread.start'):
            main.begin_softstart()
            old_token = main.power_cancel
            main.safe_power_off()
            main.begin_softstart()
            new_token = main.power_cancel
        main.SOFTSTART_SECONDS = 0
        main.pi.calls.clear()
        main.softstart_sequence(old_token)
        self.assertEqual(main.pi.calls, [])
        self.assertIs(main.power_cancel, new_token)
        self.assertTrue(main.softstart_active)

    def test_completion_switches_to_fullpower_and_reports_progress(self):
        with patch('threading.Thread.start'):
            main.begin_softstart()
        main.SOFTSTART_SECONDS = 0
        main.softstart_sequence(main.power_cancel)
        self.assertEqual(main.pi.read(main.FULLPOWER_PIN), 0)
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)
        self.assertFalse(main.softstart_active)
        self.assertEqual(main.softstart_progress, 100)
        with patch('threading.Thread.start') as start:
            main.begin_softstart()
            start.assert_not_called()

    def test_invalid_power_payloads_do_not_touch_gpio(self):
        for payload in ({'power': 'false'}, {'power': 0}, {'power': None}, {}, [], None):
            with self.subTest(payload=payload):
                response = self.client.post('/toggle_power', json=payload)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(main.pi.calls, [])

    def test_shutdown_attempts_both_relays_even_if_one_fails(self):
        main.pi.fail_pin = main.FULLPOWER_PIN
        with self.assertLogs(main.logger, level='ERROR'):
            with self.assertRaises(RuntimeError):
                main.safe_power_off()
        self.assertIn((main.SOFTSTART_PIN, 1), main.pi.calls)

    def test_worker_start_failure_cancels_and_turns_off_relays(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('cannot start')):
            with self.assertLogs(main.logger, level='ERROR'):
                response = self.client.post('/toggle_power', json={'power': True})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(main.get_power_state())
        self.assertFalse(main.softstart_active)
        self.assertIsNone(main.power_cancel)

    def test_fullpower_failure_cancels_sequence_and_attempts_shutdown(self):
        with patch('threading.Thread.start'):
            main.begin_softstart()
        token = main.power_cancel
        main.SOFTSTART_SECONDS = 0
        main.pi.fail_pin = main.FULLPOWER_PIN
        with self.assertLogs(main.logger, level='ERROR'):
            with self.assertRaises(RuntimeError):
                main.softstart_sequence(token)
        self.assertTrue(token.is_set())
        self.assertIsNone(main.power_cancel)
        self.assertFalse(main.softstart_active)
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)


if __name__ == '__main__':
    unittest.main()
