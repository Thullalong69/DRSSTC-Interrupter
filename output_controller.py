"""One process-local owner for all interrupter output modes."""
import threading
import time
import uuid

from signal_limits import SignalValidationError, MIN_T_OFF, MAX_DUTY_CYCLE


class OutputHardwareError(RuntimeError):
    pass


class ModeConflictError(RuntimeError):
    pass


class OutputController:
    PWM_MODES = frozenset(('ton_toff', 'burst', 'duty_cycle'))

    def __init__(self, *, stop_outputs, apply_pwm, fire_pulse, logger):
        self.lock = threading.RLock()
        self._stop_outputs = stop_outputs
        self._apply_pwm = apply_pwm
        self._fire_pulse = fire_pulse
        self._logger = logger
        self.mode = 'idle'
        self.midi_token = None
        self.settings = None
        self.error = None
        self.revision = 0
        self.instance_id = uuid.uuid4().hex
        self._last_on_us = 0

    def _quiet_outputs_locked(self, next_on_us, *, already_stopped=False):
        # Restarting PWM can otherwise move successive HIGH edges arbitrarily
        # close together. Keep the output LOW for a conservative duty budget
        # even when the request changes the mode or pulse width.
        if not already_stopped:
            self._stop_outputs()
        pause = max(MIN_T_OFF / 1000,
                    max(self._last_on_us, next_on_us) / (MAX_DUTY_CYCLE * 10_000))
        time.sleep(pause)

    def status(self):
        with self.lock:
            return dict(mode=self.mode, playing=self.mode == 'midi',
                        burst_active=self.mode == 'burst', error=self.error,
                        revision=self.revision,
                        instance_id=self.instance_id,
                        settings=self.settings.as_dict() if self.settings else None)

    def _invalidate_locked(self):
        if self.midi_token is not None:
            self.midi_token.set()
        self.midi_token = None
        self.settings = None

    def _fault_locked(self, error):
        self._invalidate_locked()
        self.mode = 'fault'
        self.error = 'Ausgangsfehler; Hardwarezustand prüfen'
        self.revision += 1
        self._logger.error('Ausgangssteuerung fehlgeschlagen: %s', error)
        try:
            self._stop_outputs()
        except Exception:
            self._logger.exception('Ausgang konnte nicht vollständig abgeschaltet werden')

    def _stop_locked(self):
        self._invalidate_locked()
        try:
            self._stop_outputs()
        except Exception as exc:
            self._fault_locked(exc)
            raise OutputHardwareError(self.error) from exc
        self.mode = 'idle'
        self.error = None
        self.revision += 1

    def stop(self, *, expected_mode=None, preserve_fault=False):
        with self.lock:
            if expected_mode is not None and self.mode != expected_mode:
                return False
            error = self.error if preserve_fault else None
            self._stop_locked()
            if error:
                self.mode = 'fault'
                self.error = error
                self.revision += 1
            return True

    def start_pwm(self, mode, settings):
        if mode not in self.PWM_MODES:
            raise ValueError('Invalid PWM mode')
        with self.lock:
            self._stop_locked()
            try:
                self._quiet_outputs_locked(settings.t_on_us, already_stopped=True)
                self._last_on_us = settings.t_on_us
                self._apply_pwm(settings)
            except Exception as exc:
                self._fault_locked(exc)
                raise OutputHardwareError(self.error) from exc
            self.mode = mode
            self.settings = settings
            self._last_on_us = settings.t_on_us
            self.revision += 1

    def begin_midi(self, token=None):
        with self.lock:
            if self.mode == 'midi':
                raise ModeConflictError('Wiedergabe läuft bereits')
            self._stop_locked()
            self.midi_token = token if token is not None else threading.Event()
            self.mode = 'midi'
            self.revision += 1
            return self.midi_token

    def owns_midi(self, token):
        with self.lock:
            return self.mode == 'midi' and self.midi_token is token and not token.is_set()

    def midi_pwm(self, token, settings):
        with self.lock:
            if not self.owns_midi(token):
                return False
            if self.settings == settings:
                return True
            try:
                self._quiet_outputs_locked(settings.t_on_us)
                self._last_on_us = settings.t_on_us
                self._apply_pwm(settings)
            except Exception as exc:
                self._fault_locked(exc)
                raise OutputHardwareError(self.error) from exc
            self.settings = settings
            self._last_on_us = settings.t_on_us
            self.revision += 1
            return True

    def midi_silence(self, token):
        with self.lock:
            if not self.owns_midi(token):
                return False
            try:
                self._stop_outputs()
            except Exception as exc:
                self._fault_locked(exc)
                raise OutputHardwareError(self.error) from exc
            self.settings = None
            self.revision += 1
            return True

    def midi_pulse(self, token, t_on_us):
        with self.lock:
            if not self.owns_midi(token):
                return False
            try:
                self._quiet_outputs_locked(t_on_us)
                self._last_on_us = t_on_us
                self._fire_pulse(t_on_us)
            except SignalValidationError:
                raise
            except Exception as exc:
                self._fault_locked(exc)
                raise OutputHardwareError(self.error) from exc
            self.settings = None
            self._last_on_us = t_on_us
            self.revision += 1
            return True

    def finish_midi(self, token, *, error=None):
        with self.lock:
            # An old worker must not change a newer mode, even if it failed.
            if self.midi_token is not token:
                return False
            if error is not None:
                self._fault_locked(error)
            else:
                self._stop_locked()
            return True

    def single_shot(self, t_on_us):
        with self.lock:
            self._stop_locked()
            self.mode = 'single_shot'
            self.revision += 1
            try:
                self._quiet_outputs_locked(t_on_us, already_stopped=True)
                self._last_on_us = t_on_us
                self._fire_pulse(t_on_us)
            except SignalValidationError:
                self.mode = 'idle'
                self.revision += 1
                raise
            except Exception as exc:
                self._fault_locked(exc)
                raise OutputHardwareError(self.error) from exc
            self.mode = 'idle'
            self._last_on_us = t_on_us
            self.revision += 1
