"""Constrain the custom five-byte MIDI format to the selected library."""
import os
import stat
import struct
from pathlib import Path

from signal_limits import SignalValidationError

MAX_MIDI_BYTES = 4 * 1024 * 1024


def validate_records(data):
    if not data or len(data) % 5:
        raise SignalValidationError('MIDI-Datei enthält unvollständige oder keine Ereignisse')
    for _, event, note, velocity in struct.iter_unpack('<HBBB', data):
        # Existing converted songs include standard pitch-bend records. They
        # retain their timing but do not directly change the output frequency.
        if event not in (0x80, 0x90, 0xE0) or note > 127 or velocity > 127:
            raise SignalValidationError('MIDI-Datei enthält ungültige Ereignisse')
    return data


def load_midi(directory, filename):
    if not isinstance(filename, str) or not filename or filename in ('.', '..'):
        raise SignalValidationError('Eine MIDI-Datei auswählen')
    if Path(filename).name != filename or '/' in filename or '\\' in filename or '\0' in filename:
        raise SignalValidationError('Ungültiger MIDI-Dateiname')
    # O_NOFOLLOW rejects a symlink; the descriptor is the file validated and
    # played, so replacing its pathname after validation cannot swap the data.
    try:
        descriptor = os.open(Path(directory) / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MIDI_BYTES:
                raise SignalValidationError('MIDI-Datei ist kein reguläres oder zu großes Datenformat')
            data = stream.read(MAX_MIDI_BYTES + 1)
    except SignalValidationError:
        raise
    except (OSError, ValueError):
        raise SignalValidationError('MIDI-Datei ist nicht verfügbar') from None
    if len(data) > MAX_MIDI_BYTES:
        raise SignalValidationError('MIDI-Datei ist zu groß')
    return validate_records(data)


def list_midi(directory):
    return sorted(entry.name for entry in Path(directory).iterdir()
                  if not entry.is_symlink() and entry.is_file()
                  and 0 < entry.stat().st_size <= MAX_MIDI_BYTES and entry.stat().st_size % 5 == 0)
