"""Create a private credential file interactively; never print secrets."""
import argparse
import getpass
import json
import os
import secrets
import stat
from pathlib import Path

from werkzeug.security import generate_password_hash


def load_credentials(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid != os.geteuid() or info.st_size > 16 * 1024):
            raise ValueError('Anmeldedatei muss dem Dienstbenutzer gehören und Modus 0600 haben')
        record = json.load(stream)
    if (not isinstance(record, dict) or not isinstance(record.get('password_hash'), str)
        or not record['password_hash'].startswith(('scrypt:', 'pbkdf2:'))
        or not isinstance(record.get('secret_key'), str) or len(record['secret_key']) < 64):
        raise ValueError('Anmeldedatei enthält keine gültige Konfiguration')
    return record


def create_credentials(path, password):
    if len(password) < 12:
        raise ValueError('Passwort muss mindestens 12 Zeichen lang sein')
    record = dict(password_hash=generate_password_hash(password), secret_key=secrets.token_hex(32))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(record, stream)
        stream.write('\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Anmeldung für die Steuerung konfigurieren')
    parser.add_argument('path', type=Path)
    arguments = parser.parse_args()
    password = getpass.getpass('Passwort (mindestens 12 Zeichen): ')
    if password != getpass.getpass('Passwort wiederholen: '):
        parser.error('Passwörter stimmen nicht überein')
    try:
        create_credentials(arguments.path, password)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print('Private Anmeldedatei erstellt; vorhandene Dateien werden nicht überschrieben.')
