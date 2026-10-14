"""Boundary and API regression checks using only simulated GPIOs."""
import threading
import unittest
from fractions import Fraction
from unittest.mock import patch

import test_midi as midi_tests
from test_midi import MidiGPIO
from test_power import main
from signal_limits import (
    MAX_T_ON, MIN_T_OFF, MAX_DUTY_CYCLE, SignalValidationError,
    calculate_pwm, calculate_ton_toff, calculate_duty_pwm,
)


class CalculationTests(unittest.TestCase):
    def test_fractional_bps_preserves_on_time_after_frequency_rounding(self):
        settings = calculate_pwm(1.9, 200)
        self.assertEqual(settings.frequency_hz, 1)
        self.assertEqual(settings.duty_million, 200)
        self.assertEqual(settings.t_on_us, 200)

    def test_fractional_frequency_does_not_increase_requested_duty(self):
        settings = calculate_duty_pwm(.01, 1.9)
        self.assertEqual(settings.frequency_hz, 1)
        self.assertLessEqual(settings.duty_percent, .01)
        self.assertLessEqual(settings.t_on_us, .01 * 10_000 / 1.9)

    def test_fraction_just_below_integer_rounds_down(self):
        frequency = Fraction(100) - Fraction(1, 10**30)
        self.assertEqual(calculate_pwm(frequency, 1).frequency_hz, 99)

    def test_requested_ton_toff_duty_is_checked_before_rounding(self):
        with self.assertRaises(SignalValidationError):
            calculate_ton_toff(200, 19.7999)
        self.assertEqual(calculate_ton_toff(200, 19.8).t_on_us, 200)

    def test_midi_settings_obey_all_limits_and_never_lengthen_pulse(self):
        for frequency in (1, 1.9, 50, 50.9, 99.999, 130.812, 195.997, 198, 199, 199.9):
            for requested_on in (1, 50, 100, 150, 200):
                with self.subTest(frequency=frequency, t_on=requested_on):
                    settings = calculate_pwm(frequency, requested_on, reduce_on_time=True)
                    self.assertGreater(settings.duty_million, 0)
                    # Independent bounds on the actual integer command.
                    on = Fraction(settings.duty_million, settings.frequency_hz)
                    off = Fraction(1_000_000 - settings.duty_million, settings.frequency_hz)
                    self.assertLessEqual(on, min(MAX_T_ON, requested_on))
                    self.assertGreaterEqual(off, MIN_T_OFF * 1000)
                    self.assertLessEqual(settings.duty_million, MAX_DUTY_CYCLE * 10_000)

    def test_frequency_without_room_for_pause_cannot_be_clamped(self):
        for frequency in (200, 440, 4186, 1e300):
            with self.subTest(frequency=frequency):
                with self.assertRaises(SignalValidationError):
                    calculate_pwm(frequency, 100, reduce_on_time=True)

    def test_sub_hertz_frequency_is_rejected_instead_of_turning_off_pwm(self):
        for frequency in (0, -1, .999999):
            with self.subTest(frequency=frequency):
                with self.assertRaises(SignalValidationError):
                    calculate_pwm(frequency, 100)


class LimitAPITests(unittest.TestCase):
    def setUp(self):
        main.pi = MidiGPIO()
        main.output_controller = main.create_output_controller()
        main.MIDI_MAX_T_ON = 100
        main.last_pulse_finished_at = None
        main.last_pulse_t_on = 0
        self.client = main.app.test_client()

    def assert_no_output(self):
        self.assertEqual(main.pi.pwm_calls, [])
        self.assertEqual(main.pi.trigger_calls, [])
        self.assertEqual(main.pi.calls, [])

    def test_ton_toff_rejects_original_limit_bypass(self):
        response = self.client.post('/set_ton_toff', data={'t_on': 1000, 't_off': 1})
        self.assertEqual(response.status_code, 400)
        self.assert_no_output()

    def test_duty_route_rejects_original_fifty_percent_bypass(self):
        response = self.client.post('/set_duty_cycle', data={'duty_cycle': 50, 'frequency': 10000})
        self.assertEqual(response.status_code, 400)
        self.assert_no_output()

    def test_all_manual_pwm_routes_enforce_pause_and_duty(self):
        for route, data in (
            ('/set_ton_toff', {'t_on': 1, 't_off': 4.99}),
            ('/set_ton_toff', {'t_on': 200, 't_off': 5}),
            ('/set_burst', {'t_on': 200, 'bps': 51}),
            ('/set_burst', {'t_on': 1, 'bps': 200}),
            ('/set_duty_cycle', {'duty_cycle': .01, 'frequency': 1000}),
            ('/set_duty_cycle', {'duty_cycle': 1.001, 'frequency': 100}),
            ('/set_duty_cycle', {'duty_cycle': -1, 'frequency': 100}),
        ):
            with self.subTest(route=route, data=data):
                self.assertEqual(self.client.post(route, data=data).status_code, 400)
                self.assert_no_output()

    def test_invalid_numbers_return_400_before_gpio_access(self):
        endpoints = (
            ('/set_ton_toff', {'t_on': 1, 't_off': 50}),
            ('/set_burst', {'t_on': 100, 'bps': 10}),
            ('/set_duty_cycle', {'duty_cycle': .1, 'frequency': 100}),
            ('/single_shot', {'t_on': 100}),
            ('/set_midi_max_t_on', {'max_t_on': 100}),
        )
        for route, valid in endpoints:
            for field in valid:
                for value in ('nan', 'inf', '-inf', '1e1000', 'bad', ''):
                    with self.subTest(route=route, field=field, value=value):
                        data = dict(valid, **{field: value})
                        response = self.client.post(route, data=data)
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.json['status'], 'error')
                        self.assert_no_output()
                missing = dict(valid)
                del missing[field]
                self.assertEqual(self.client.post(route, data=missing).status_code, 400)
        self.assert_no_output()

    def test_frequency_zero_is_validation_error_instead_of_server_error(self):
        response = self.client.post('/set_duty_cycle', data={'duty_cycle': .1, 'frequency': 0})
        self.assertEqual(response.status_code, 400)
        self.assert_no_output()

    def test_burst_rounding_returns_applied_settings(self):
        response = self.client.post('/set_burst', data={'t_on': 200, 'bps': 1.9})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 1, 200))
        self.assertEqual(response.json['settings']['t_on_us'], 200)
        self.assertEqual(response.json['settings']['frequency_hz'], 1)

    def test_ton_toff_response_reports_milliseconds_and_applied_frequency(self):
        response = self.client.post('/set_ton_toff', data={'t_on': 200, 't_off': 19.8})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 50, 10000))
        self.assertEqual(response.json['settings']['t_off_ms'], 19.8)
        self.assertIn('19.800 ms', response.json['message'])

    def test_safe_duty_request_is_applied(self):
        response = self.client.post('/set_duty_cycle', data={'duty_cycle': 1, 'frequency': 100})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 100, 10000))

    def test_explicit_zero_can_still_stop_without_other_fields(self):
        for route, field in (('/set_ton_toff', 't_on'), ('/set_burst', 't_on'), ('/set_duty_cycle', 'duty_cycle')):
            with self.subTest(route=route):
                token = threading.Event()
                main.output_controller.begin_midi(token)
                response = self.client.post(route, data={field: 0})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(token.is_set())
                self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))

    def test_single_shot_rejects_invalid_pulse_without_changing_pin(self):
        for t_on in (-1, 0, 200.01, 201, 1.5):
            with self.subTest(t_on=t_on):
                response = self.client.post('/single_shot', data={'t_on': t_on})
                self.assertEqual(response.status_code, 400)
                self.assert_no_output()

    def test_repeated_single_shots_cannot_bypass_pause_or_duty(self):
        with patch('time.monotonic', return_value=0):
            first = self.client.post('/single_shot', data={'t_on': 100})
            second = self.client.post('/single_shot', data={'t_on': 100})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(main.pi.trigger_calls), 1)
        with patch('time.monotonic', return_value=.011):
            third = self.client.post('/single_shot', data={'t_on': 100})
        self.assertEqual(third.status_code, 200)
        self.assertEqual(len(main.pi.trigger_calls), 2)

    def test_pulse_width_change_does_not_bypass_duty_limit(self):
        with patch('time.monotonic', return_value=0):
            self.assertEqual(self.client.post('/single_shot', data={'t_on': 1}).status_code, 200)
        with patch('time.monotonic', return_value=.006):
            response = self.client.post('/single_shot', data={'t_on': 200})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(main.pi.trigger_calls), 1)

    def test_pulses_above_trigger_limit_use_wave_api(self):
        with patch.object(main.pi, 'wave_add_generic') as add, patch.object(main.pi, 'wave_create', return_value=7), patch.object(main.pi, 'wave_send_once') as send:
            response = self.client.post('/single_shot', data={'t_on': 200})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.pi.trigger_calls, [])
        self.assertEqual(add.call_args.args[0][0].delay, 200)
        send.assert_called_once_with(7)

    def test_midi_limit_changes_are_validated(self):
        for value in (0, -1, 201, 100.5):
            self.assertEqual(self.client.post('/set_midi_max_t_on', data={'max_t_on': value}).status_code, 400)
        self.assertEqual(main.MIDI_MAX_T_ON, 100)
        self.assertEqual(self.client.post('/set_midi_max_t_on', data={'max_t_on': 200}).status_code, 200)
        self.assertEqual(main.MIDI_MAX_T_ON, 200)

    def test_unused_pwm_helpers_do_not_bypass_limits(self):
        with self.assertRaises(SignalValidationError):
            main.set_pwm(1000, 1)
        with self.assertRaises(SignalValidationError):
            main.send_pulse(200, 1000)
        self.assert_no_output()

    def test_cw_removed_from_api_and_page(self):
        self.assertEqual(self.client.post('/start_cw').status_code, 404)
        self.assertEqual(self.client.post('/stop_cw').status_code, 404)
        page = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('cw-mode', page)
        self.assertNotIn('cw_start_button', page)
        self.assertNotIn('cw_enable', page)
        self.assert_no_output()


class MidiBoundsTests(unittest.TestCase):
    setUp = midi_tests.MidiTests.setUp
    tearDown = midi_tests.MidiTests.tearDown
    file = midi_tests.MidiTests.file
    activate = midi_tests.MidiTests.activate

    def test_high_midi_note_is_silent_and_stops_previous_note(self):
        token = threading.Event()
        self.activate(token)
        with self.assertLogs(main.logger, level='WARNING'):
            main.play_midi_file(self.file((0, 0x90, 48, 100), (0, 0x90, 69, 100)), token)
        self.assertIn((main.INTERRUPTER_PIN, 130, 10000), main.pi.pwm_calls)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))
        self.assertEqual(sum(bool(freq) for _, freq, _ in main.pi.pwm_calls), 1)

    def test_all_midi_pitches_are_bounded_or_rejected(self):
        accepted = 0
        rejected = 0
        for note in range(128):
            frequency = main.midi_note_to_frequency(note)
            try:
                duty = main.calculate_max_duty_cycle(frequency, 200)
            except SignalValidationError:
                rejected += 1
                self.assertGreaterEqual(frequency, 200)
                continue
            accepted += 1
            hz = int(frequency)
            self.assertLessEqual(Fraction(duty, hz), 200)
            self.assertLessEqual(duty, 10000)
            self.assertGreaterEqual(Fraction(1_000_000 - duty, hz), 5000)
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)

    def test_trigger_mode_enforces_repeat_interval(self):
        main.FORCE_GPIO_TRIGGER = True
        token = threading.Event()
        self.activate(token)
        with patch('time.monotonic', return_value=0), self.assertLogs(main.logger, level='WARNING'):
            main.play_midi_file(self.file((0, 0x90, 48, 100), (0, 0x90, 50, 100)), token)
        self.assertEqual(len(main.pi.trigger_calls), 1)


if __name__ == '__main__':
    unittest.main()
