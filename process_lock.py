"""Reject a second GPIO-owning process before it connects to pigpiod."""
import fcntl
import os


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self.descriptor = None

    def acquire(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise RuntimeError('Ein anderer Prozess besitzt bereits die GPIO-Steuerung') from None
        self.descriptor = descriptor

    def release(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
