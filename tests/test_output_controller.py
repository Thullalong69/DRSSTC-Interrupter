"""Mode ownership, failure, and concurrency checks with simulated GPIOs."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_midi as midi_tests
from test_midi import MidiGPIO
from test_power import main
from signal_limits import calculate_pwm


class ModeTests(unittest.TestCase):
    setUp = midi_tests.MidiTests.setUp
    tearDown = midi_tests.MidiTests.tearDown
    file = midi_tests.MidiTests.file
    activate = midi_tests.MidiTests.activate
    worker = midi_tests.MidiTests.worker

    def assert_mode(self, mode):
        state = self.client.get('/output_status').json
        self.assertEqual(state['mode'], mode)
        self.assertEqual(self.client.get('/burst_status').json['active'], mode == 'burst')
        self.assertEqual(self.client.get('/playback_status').json['playing'], mode == 'midi')
        return state

    def test_every_pwm_mode_cancels_old_midi_and_prevents_late_cleanup(self):
        for route, data, mode in (
            ('/set_burst', {'bps': 30, 't_on': 100}, 'burst'),
            ('/set_ton_toff', {'t_on': 100, 't_off': 50}, 'ton_toff'),
            ('/set_duty_cycle', {'duty_cycle': .1, 'frequency': 100}, 'duty_cycle'),
        ):
            with self.subTest(mode=mode):
                main.output_controller.stop()
                token = midi_tests.PausedWait()
                old_worker = self.worker(self.file((0, 0x90, 48, 100)), token)
                self.assertTrue(token.waiting.wait(1))
                response = self.client.post(route, data=data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(token.is_set())
                self.assert_mode(mode)
                calls = list(main.pi.pwm_calls)
                token.release.set()
                old_worker.join(timeout=1)
                self.assertFalse(old_worker.is_alive())
                self.assertEqual(main.pi.pwm_calls, calls)
                self.assert_mode(mode)

    def test_pwm_transitions_keep_only_one_status_active(self):
        for route, data, mode in (
            ('/set_burst', {'bps': 30, 't_on': 100}, 'burst'),
            ('/set_ton_toff', {'t_on': 100, 't_off': 50}, 'ton_toff'),
            ('/set_duty_cycle', {'duty_cycle': .1, 'frequency': 100}, 'duty_cycle'),
            ('/set_burst', {'bps': 10, 't_on': 200}, 'burst'),
        ):
            with self.subTest(mode=mode):
                response = self.client.post(route, data=data)
                self.assertEqual(response.status_code, 200)
                self.assert_mode(mode)
                self.assertEqual(response.json['output']['mode'], mode)
                self.assertEqual(response.json['output']['settings'], response.json['settings'])

    def test_midi_start_stops_burst_before_reserving_playback(self):
        self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        with patch('threading.Thread.start'):
            response = self.client.post('/play_midi', data={'midi_file': Path(self.file((0, 0x80, 48, 0))).name})
        self.assertEqual(response.status_code, 200)
        state = self.assert_mode('midi')
        self.assertIsNone(state['settings'])
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))

    def test_delayed_midi_stop_does_not_stop_new_burst(self):
        token = threading.Event()
        self.activate(token)
        self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        self.assertTrue(token.is_set())
        calls = list(main.pi.pwm_calls)
        response = self.client.post('/stop_midi')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.pi.pwm_calls, calls)
        self.assert_mode('burst')

    def test_global_stop_clears_mode_settings_and_token(self):
        token = threading.Event()
        self.activate(token)
        main.output_controller.midi_pwm(token, calculate_pwm(130, 50))
        response = self.client.post('/stop_output')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(token.is_set())
        self.assertIsNone(main.output_controller.midi_token)
        state = self.assert_mode('idle')
        self.assertIsNone(state['settings'])
        self.assertIsNone(state['error'])
        self.assertEqual(main.pi.read(main.INTERRUPTER_PIN), 0)

    def test_invalid_request_leaves_existing_mode_and_token_untouched(self):
        token = threading.Event()
        self.activate(token)
        calls = list(main.pi.pwm_calls)
        response = self.client.post('/set_burst', data={'bps': 1000, 't_on': 200})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(token.is_set())
        self.assertIs(main.output_controller.midi_token, token)
        self.assertEqual(main.pi.pwm_calls, calls)
        self.assert_mode('midi')

    def test_single_shot_cancels_playback_and_returns_to_idle(self):
        token = midi_tests.PausedWait()
        old_worker = self.worker(self.file((0, 0x90, 48, 100)), token)
        self.assertTrue(token.waiting.wait(1))
        response = self.client.post('/single_shot', data={'t_on': 100})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(token.is_set())
        calls = list(main.pi.pwm_calls)
        token.release.set()
        old_worker.join(timeout=1)
        self.assertFalse(old_worker.is_alive())
        self.assertEqual(main.pi.pwm_calls, calls)
        self.assertEqual(len(main.pi.trigger_calls), 1)
        self.assert_mode('idle')

    def test_power_off_stops_mode_and_cancels_waiting_midi(self):
        token = midi_tests.PausedWait()
        worker = self.worker(self.file((0, 0x90, 48, 100)), token)
        self.assertTrue(token.waiting.wait(1))
        response = self.client.post('/toggle_power', json={'power': False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['power'])
        self.assertTrue(token.is_set())
        self.assertEqual(main.pi.read(main.SOFTSTART_PIN), 1)
        self.assertEqual(main.pi.read(main.FULLPOWER_PIN), 1)
        calls = list(main.pi.pwm_calls)
        token.release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(main.pi.pwm_calls, calls)
        self.assert_mode('idle')

    def test_relay_failure_does_not_prevent_interrupter_stop(self):
        self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        main.pi.fail_pin = main.FULLPOWER_PIN
        try:
            with self.assertLogs(main.logger, level='ERROR'):
                response = self.client.post('/toggle_power', json={'power': False})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(main.pi.read(main.INTERRUPTER_PIN), 0)
            self.assert_mode('idle')
        finally:
            main.pi.fail_pin = None

    def test_old_worker_error_cannot_fault_new_mode(self):
        token = threading.Event()
        self.activate(token)
        self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        calls = list(main.pi.pwm_calls)
        self.assertFalse(main.output_controller.finish_midi(token, error=RuntimeError('old worker failed')))
        self.assertEqual(main.pi.pwm_calls, calls)
        self.assertIsNone(self.assert_mode('burst')['error'])

    def test_pwm_failure_reports_fault_and_stops_output(self):
        original = main.pi.hardware_PWM
        def fail_on_active(pin, frequency, duty):
            if frequency:
                raise RuntimeError('PWM failed')
            return original(pin, frequency, duty)
        with patch.object(main.pi, 'hardware_PWM', fail_on_active), self.assertLogs(main.logger, level='ERROR'):
            response = self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json['output']['mode'], 'fault')
        state = self.assert_mode('fault')
        self.assertIsNotNone(state['error'])
        self.assertIsNone(state['settings'])
        self.assertEqual(main.pi.read(main.INTERRUPTER_PIN), 0)
        self.client.post('/stop_output')
        self.assert_mode('idle')

    def test_shutdown_failure_still_attempts_pin_low_and_reports_fault(self):
        with patch.object(main.pi, 'wave_tx_stop', side_effect=RuntimeError('wave stop failed')), self.assertLogs(main.logger, level='ERROR'):
            response = self.client.post('/stop_output')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(main.pi.read(main.INTERRUPTER_PIN), 0)
        self.assert_mode('fault')

    def test_malformed_midi_reports_fault_in_shared_status(self):
        token = threading.Event()
        self.activate(token)
        with self.assertLogs(main.logger, level='ERROR'):
            main.play_midi_file('/nonexistent-review-midi-file', token)
        self.assertTrue(token.is_set())
        self.assertIsNotNone(self.assert_mode('fault')['error'])

    def test_mode_change_waits_low_for_previous_and_next_pulse_budget(self):
        settings = calculate_pwm(50, 200)
        with patch('time.sleep') as sleep:
            main.output_controller.start_pwm('burst', settings)
            main.output_controller.start_pwm('ton_toff', calculate_pwm(100, 1))
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], .020)
        self.assertGreaterEqual(sleep.call_args_list[1].args[0], .020)
        # Both outputs must be preceded by an explicit OFF command.
        self.assertEqual(main.pi.pwm_calls, [
            (main.INTERRUPTER_PIN, 0, 0), (main.INTERRUPTER_PIN, 50, 10000),
            (main.INTERRUPTER_PIN, 0, 0), (main.INTERRUPTER_PIN, 100, 100),
        ])

    def test_same_midi_pwm_does_not_retrigger_or_reset_phase(self):
        token = threading.Event()
        self.activate(token)
        settings = calculate_pwm(130, 50)
        main.output_controller.midi_pwm(token, settings)
        calls = list(main.pi.pwm_calls)
        main.output_controller.midi_pwm(token, settings)
        self.assertEqual(main.pi.pwm_calls, calls)

    def test_two_concurrent_pwm_requests_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        responses = []
        original = main.pi.hardware_PWM
        def blocked_first(pin, frequency, duty):
            if frequency == 30:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test did not release first request')
            return original(pin, frequency, duty)
        def first():
            with main.app.test_client() as client:
                responses.append(client.post('/set_burst', data={'bps': 30, 't_on': 100}).status_code)
        def second():
            with main.app.test_client() as client:
                responses.append(client.post('/set_duty_cycle', data={'frequency': 100, 'duty_cycle': .1}).status_code)
            second_done.set()
        first_worker = threading.Thread(target=first)
        second_worker = threading.Thread(target=second)
        self.workers.extend((first_worker, second_worker))
        with patch.object(main.pi, 'hardware_PWM', blocked_first):
            first_worker.start()
            self.assertTrue(entered.wait(1))
            second_worker.start()
            try:
                self.assertFalse(second_done.wait(.05))
            finally:
                release.set()
            first_worker.join(timeout=1)
            second_worker.join(timeout=1)
        self.assertFalse(first_worker.is_alive())
        self.assertFalse(second_worker.is_alive())
        self.assertEqual(responses, [200, 200])
        self.assert_mode('duty_cycle')
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 100, 1000))

    def test_revisions_increase_and_controller_restart_has_new_identity(self):
        before = self.assert_mode('idle')
        self.client.post('/set_burst', data={'bps': 30, 't_on': 100})
        active = self.assert_mode('burst')
        self.client.post('/stop_output')
        after = self.assert_mode('idle')
        self.assertLess(before['revision'], active['revision'])
        self.assertLess(active['revision'], after['revision'])
        self.assertEqual(before['instance_id'], after['instance_id'])
        self.assertNotEqual(after['instance_id'], main.create_output_controller().status()['instance_id'])


if __name__ == '__main__':
    unittest.main()
