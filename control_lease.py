"""Only the controlling browser may renew the hardware watchdog."""
import threading
import time
from contextlib import contextmanager


class LeaseConflictError(RuntimeError):
    pass


class ControlLease:
    def __init__(self, stop_hardware, timeout=5, clock=time.monotonic):
        self.lock = threading.RLock()
        self.stop_hardware = stop_hardware
        self.timeout = timeout
        self.clock = clock
        self.owner = None
        self.deadline = None
        self.shutdown_pending = False

    def _expire_locked(self):
        if not self.shutdown_pending and (self.owner is None or self.clock() < self.deadline):
            return False
        # Clear ownership even if hardware shutdown fails; no renewal may
        # silently restart the expired operation.
        self.owner = None
        self.deadline = None
        self.shutdown_pending = True
        self.stop_hardware('Browser-Heartbeat abgelaufen')
        self.shutdown_pending = False
        return True

    @contextmanager
    def command(self, operator):
        with self.lock:
            self._expire_locked()
            if not operator:
                raise LeaseConflictError('Anmeldung erforderlich')
            if self.owner is not None and self.owner != operator:
                raise LeaseConflictError('Ein anderes Fenster steuert die Anlage')
            self.owner = operator
            self.deadline = self.clock() + self.timeout
            yield

    def heartbeat(self, operator):
        with self.lock:
            self._expire_locked()
            if self.owner is not None and self.owner != operator:
                raise LeaseConflictError('Ein anderes Fenster steuert die Anlage')
            if self.owner is not None:
                self.deadline = self.clock() + self.timeout
            return self.status(operator)

    def status(self, operator):
        with self.lock:
            return dict(controlling=self.owner is not None and self.owner == operator,
                        occupied=self.owner is not None,
                        shutdown_pending=self.shutdown_pending,
                        heartbeat_timeout_s=self.timeout)

    def check_expired(self):
        with self.lock:
            return self._expire_locked()

    def power_off(self, reason):
        with self.lock:
            self.owner = None
            self.deadline = None
            self.shutdown_pending = True
            self.stop_hardware(reason)
            self.shutdown_pending = False

    def logout(self, operator):
        with self.lock:
            if self.owner is not None and self.owner == operator:
                self.power_off('Steuerndes Fenster abgemeldet')
