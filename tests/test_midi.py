"""MIDI cancellation regression tests; GPIO access is always simulated."""
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_power import FakeGPIO, main


class MidiGPIO(FakeGPIO):
    def __init__(self):
        super().__init__()
        self.pwm_calls = []
        self.trigger_calls = []
        self.output_started = threading.Event()

    def hardware_PWM(self, pin, frequency, duty):
        self.pwm_calls.append((pin, frequency, duty))
        if frequency and duty:
            self.output_started.set()
        return 0

    def gpio_trigger(self, *args):
        self.trigger_calls.append(args)
        return 0


class ObservedCancel(threading.Event):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


class PausedWait(ObservedCancel):
    """Hold the worker at the race between wait() and acquiring the output lock."""
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        if not self.release.wait(2):
            raise RuntimeError('test did not release the worker')
        return False


class MidiTests(unittest.TestCase):
    def setUp(self):
        main.pi = MidiGPIO()
        main.output_controller = main.create_output_controller()
        main.FORCE_GPIO_TRIGGER = False
        main.MIDI_MAX_T_ON = 100
        main.last_pulse_finished_at = None
        main.last_pulse_t_on = 0
        self.client = main.app.test_client()
        self.files = tempfile.TemporaryDirectory()
        self.old_midi_dir = main.MIDI_FILES_DIR
        main.MIDI_FILES_DIR = Path(self.files.name)
        main.MIDI_PITCH = 0
        self.workers = []
        self.paused = []

    def tearDown(self):
        main._stop_all_outputs()
        for cancel in self.paused:
            cancel.release.set()
        for worker in self.workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        main.MIDI_FILES_DIR = self.old_midi_dir
        self.files.cleanup()

    def file(self, *records):
        path = Path(self.files.name) / f'{len(list(Path(self.files.name).iterdir()))}.dat'
        path.write_bytes(b''.join(struct.pack('<HBBB', *record) for record in records))
        return str(path)

    def activate(self, cancel):
        main.output_controller.begin_midi(cancel)
        main.pi.pwm_calls.clear()
        main.pi.calls.clear()

    def worker(self, path, cancel):
        self.activate(cancel)
        if isinstance(cancel, PausedWait):
            self.paused.append(cancel)
        worker = threading.Thread(target=main.play_midi_file, args=(path, cancel))
        self.workers.append(worker)
        worker.start()
        return worker

    def test_stop_interrupts_long_delay_without_starting_output(self):
        cancel = ObservedCancel()
        worker = self.worker(self.file((25095, 0x90, 48, 100)), cancel)
        self.assertTrue(cancel.waiting.wait(1))
        response = self.client.post('/stop_midi')
        self.assertEqual(response.status_code, 200)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancel.is_set())
        self.assertFalse(main.output_controller.status()['playing'])
        self.assertTrue(all(freq == 0 for _, freq, _ in main.pi.pwm_calls))
        self.assertEqual(main.pi.read(main.INTERRUPTER_PIN), 0)

    def test_stop_after_wait_before_gpio_cannot_resume_output(self):
        cancel = PausedWait()
        worker = self.worker(self.file((0, 0x90, 48, 100)), cancel)
        self.assertTrue(cancel.waiting.wait(1))
        self.client.post('/stop_midi')
        calls_after_stop = list(main.pi.pwm_calls)
        cancel.release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(main.pi.pwm_calls, calls_after_stop)

    def test_old_worker_cannot_affect_restarted_playback(self):
        old = PausedWait()
        worker = self.worker(self.file((0, 0x90, 48, 100)), old)
        self.assertTrue(old.waiting.wait(1))
        self.client.post('/stop_midi')
        with patch('threading.Thread.start'):
            response = self.client.post('/play_midi', data={'midi_file': Path(self.file((0, 0x80, 48, 0))).name})
        self.assertEqual(response.status_code, 200)
        new = main.output_controller.midi_token
        main.pi.hardware_PWM(main.INTERRUPTER_PIN, 130, 10000)
        calls = list(main.pi.pwm_calls)
        old.release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertIs(main.output_controller.midi_token, new)
        self.assertTrue(main.output_controller.status()['playing'])
        self.assertFalse(new.is_set())
        self.assertEqual(main.pi.pwm_calls, calls)

    def test_stop_turns_off_active_note_and_wakes_worker(self):
        cancel = ObservedCancel()
        worker = self.worker(self.file((0, 0x90, 48, 100), (25095, 0x80, 48, 0)), cancel)
        self.assertTrue(main.pi.output_started.wait(1))
        self.client.post('/stop_midi')
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))
        self.assertFalse(main.output_controller.status()['playing'])

    def test_global_stop_cancels_pending_midi_without_stopping_new_pwm(self):
        cancel = PausedWait()
        worker = self.worker(self.file((0, 0x90, 48, 100)), cancel)
        self.assertTrue(cancel.waiting.wait(1))
        main._stop_all_outputs()
        response = self.client.post('/set_ton_toff', data={'t_on': 1, 't_off': 50})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(cancel.is_set())
        calls = list(main.pi.pwm_calls)
        cancel.release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 19, 19))
        self.assertEqual(main.pi.pwm_calls, calls)

    def test_duplicate_play_is_rejected_without_new_worker(self):
        with patch('threading.Thread.start') as start:
            first = self.client.post('/play_midi', data={'midi_file': Path(self.file((0, 0x80, 48, 0))).name})
            token = main.output_controller.midi_token
            second = self.client.post('/play_midi', data={'midi_file': Path(self.file((0, 0x80, 48, 0))).name})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(start.call_count, 1)
        self.assertIs(main.output_controller.midi_token, token)

    def test_thread_start_failure_resets_status(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('cannot start')):
            with self.assertLogs(main.logger, level='ERROR'):
                response = self.client.post('/play_midi', data={'midi_file': Path(self.file((0, 0x80, 48, 0))).name})
        self.assertEqual(response.status_code, 500)
        self.assertFalse(main.output_controller.status()['playing'])
        self.assertIsNone(main.output_controller.midi_token)

    def test_end_of_file_turns_output_off_and_clears_status(self):
        cancel = threading.Event()
        self.activate(cancel)
        main.play_midi_file(self.file((0, 0x90, 48, 100)), cancel)
        self.assertIn((main.INTERRUPTER_PIN, 130, 10000), main.pi.pwm_calls)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))
        self.assertFalse(main.output_controller.status()['playing'])
        self.assertIsNone(main.output_controller.midi_token)

    def test_zero_velocity_note_on_stops_active_note(self):
        cancel = threading.Event()
        self.activate(cancel)
        main.play_midi_file(self.file((0, 0x90, 48, 100), (0, 0x90, 48, 0)), cancel)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))
        self.assertEqual(sum(bool(freq) for _, freq, _ in main.pi.pwm_calls), 1)

    def test_cancelled_trigger_mode_does_not_fire(self):
        main.FORCE_GPIO_TRIGGER = True
        cancel = PausedWait()
        worker = self.worker(self.file((0, 0x90, 48, 100)), cancel)
        self.assertTrue(cancel.waiting.wait(1))
        self.client.post('/stop_midi')
        cancel.release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(main.pi.trigger_calls, [])

    def test_malformed_file_stops_output_and_clears_status(self):
        path = Path(self.file((0, 0x90, 48, 100)))
        path.write_bytes(path.read_bytes() + b'x')
        cancel = threading.Event()
        self.activate(cancel)
        with self.assertLogs(main.logger, level='ERROR'):
            main.play_midi_file(str(path), cancel)
        self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 0, 0))
        self.assertFalse(main.output_controller.status()['playing'])

    def test_waits_use_cumulative_deadlines(self):
        cancel = threading.Event()
        self.activate(cancel)
        # Processing of the first event consumed 50 ms. The next wait should
        # be 50 ms, instead of shifting the scheduled second event by 50 ms.
        with patch('time.monotonic', side_effect=[0, 0, .15]), patch.object(cancel, 'wait', return_value=False) as wait:
            main.play_midi_file(self.file((100, 0x80, 48, 0), (100, 0x80, 48, 0)), cancel)
        self.assertAlmostEqual(wait.call_args_list[0].args[0], .1)
        self.assertAlmostEqual(wait.call_args_list[1].args[0], .05)


if __name__ == '__main__':
    unittest.main()
