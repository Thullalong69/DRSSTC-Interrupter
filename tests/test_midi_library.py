import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from midi_library import MAX_MIDI_BYTES, list_midi, load_midi
from signal_limits import SignalValidationError, calculate_pwm
from test_midi import MidiGPIO
from test_power import main


class MidiLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = struct.pack('<HBBB', 0, 0x90, 48, 100) + struct.pack('<HBBB', 10, 0x80, 48, 0)
        (self.root / 'song.dat').write_bytes(self.data)
        self.previous_dir = main.MIDI_FILES_DIR
        main.MIDI_FILES_DIR = self.root
        main.pi = MidiGPIO()
        main.output_controller = main.create_output_controller()
        self.client = main.app.test_client()

    def tearDown(self):
        main.output_controller.stop()
        main.MIDI_FILES_DIR = self.previous_dir
        self.directory.cleanup()

    def test_traversal_absolute_and_invalid_names_are_rejected(self):
        for filename in ('../main.py', '/etc/passwd', 'sub/file', 'sub\\file', '\0', '', '.', '..', None):
            with self.subTest(filename=filename):
                with self.assertRaises(SignalValidationError):
                    load_midi(self.root, filename)

    def test_symlinks_and_fifo_are_rejected_without_blocking(self):
        (self.root / 'link').symlink_to(self.root / 'song.dat')
        os.mkfifo(self.root / 'fifo')
        for name in ('link', 'fifo'):
            with self.assertRaises(SignalValidationError):
                load_midi(self.root, name)
        self.assertNotIn('link', list_midi(self.root))
        self.assertNotIn('fifo', list_midi(self.root))

    def test_empty_truncated_invalid_and_oversized_records_are_rejected(self):
        for data in (b'', b'x', struct.pack('<HBBB', 0, 0x91, 48, 100),
                     struct.pack('<HBBB', 0, 0x90, 128, 100),
                     struct.pack('<HBBB', 0, 0x90, 48, 128)):
            (self.root / 'bad').write_bytes(data)
            with self.assertRaises(SignalValidationError):
                load_midi(self.root, 'bad')
        with (self.root / 'large').open('wb') as stream:
            stream.truncate(MAX_MIDI_BYTES + 1)
        with self.assertRaises(SignalValidationError):
            load_midi(self.root, 'large')

    def test_exact_filenames_including_extensions_are_listed_and_played(self):
        self.assertEqual(self.client.get('/get_midi_files').json['files'], ['song.dat'])
        with patch('threading.Thread') as thread:
            response = self.client.post('/play_midi', data={'midi_file': 'song.dat'})
        self.assertEqual(response.status_code, 200)
        payload = thread.call_args.kwargs['args'][0]
        self.assertEqual(payload, self.data)
        (self.root / 'song.dat').write_bytes(b'replaced')
        self.assertEqual(payload, self.data)

    def test_invalid_file_does_not_cancel_existing_burst(self):
        main.output_controller.start_pwm('burst', calculate_pwm(30, 100))
        main.pi.pwm_calls.clear()
        state = main.output_controller.status()
        response = self.client.post('/play_midi', data={'midi_file': '../main.py'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(main.output_controller.status(), state)
        self.assertEqual(main.pi.pwm_calls, [])

    def test_existing_library_validates_and_empty_placeholder_is_hidden(self):
        root = Path(main.__file__).parent / 'data' / 'midi-files'
        names = list_midi(root)
        self.assertEqual(len(list(root.iterdir())), 87)
        self.assertEqual(len(names), 86)
        self.assertNotIn('TheEmptinessMachine', names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(load_midi(root, name))

    def test_pitch_is_validated_and_applied_to_future_notes(self):
        previous_pitch = main.MIDI_PITCH
        try:
            for value in (-13, 13, .5, 'nan'):
                self.assertEqual(self.client.post('/update_pitch', data={'pitch': value}).status_code, 400)
            response = self.client.post('/update_pitch', data={'pitch': -12})
            self.assertEqual(response.status_code, 200)
            token = main.output_controller.begin_midi()
            main.pi.pwm_calls.clear()
            main.play_midi_file(struct.pack('<HBBB', 0, 0x90, 60, 100), token)
            self.assertIn((main.INTERRUPTER_PIN, 130, 10000), main.pi.pwm_calls)
        finally:
            main.MIDI_PITCH = previous_pitch

    def test_reducing_midi_on_time_updates_current_note_immediately(self):
        previous_on = main.MIDI_MAX_T_ON
        try:
            token = main.output_controller.begin_midi()
            main.output_controller.midi_pwm(token, calculate_pwm(130, 100, reduce_on_time=True))
            response = self.client.post('/set_midi_max_t_on', data={'max_t_on': 10})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(main.pi.pwm_calls[-1], (main.INTERRUPTER_PIN, 130, 1300))
            self.assertEqual(main.output_controller.status()['settings']['t_on_us'], 10)
        finally:
            main.MIDI_MAX_T_ON = previous_on


if __name__ == '__main__':
    unittest.main()
