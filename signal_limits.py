"""Nominal pigpio PWM calculations, with explicit units and shared limits."""
import math
from dataclasses import dataclass
from fractions import Fraction

MAX_T_ON = 200  # microseconds
MIN_T_OFF = 5  # milliseconds
MAX_DUTY_CYCLE = 1  # percent


class SignalValidationError(ValueError):
    pass


class PulseRateLimitError(SignalValidationError):
    pass


def finite_number(value, name):
    if isinstance(value, bool):
        raise SignalValidationError(f"{name} muss eine endliche Zahl sein")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise SignalValidationError(f"{name} muss eine endliche Zahl sein") from None
    if not math.isfinite(number):
        raise SignalValidationError(f"{name} muss eine endliche Zahl sein")
    return number


def validate_t_on(value, *, allow_zero=False):
    number = finite_number(value, 't_ON')
    if not (0 <= number <= MAX_T_ON) or (number == 0 and not allow_zero):
        interval = f'zwischen 0 und {MAX_T_ON}' if allow_zero else f'> 0 und höchstens {MAX_T_ON}'
        raise SignalValidationError(f't_ON muss {interval} µs sein')
    return number


def _fraction(value):
    return value if isinstance(value, Fraction) else Fraction(str(value))


@dataclass(frozen=True)
class PWMSettings:
    frequency_hz: int
    duty_million: int

    @property
    def t_on_us(self):
        return self.duty_million / self.frequency_hz

    @property
    def t_off_ms(self):
        return (1_000_000 - self.duty_million) / self.frequency_hz / 1000

    @property
    def duty_percent(self):
        return self.duty_million / 10_000

    def as_dict(self):
        return dict(frequency_hz=self.frequency_hz, duty_percent=self.duty_percent,
                    t_on_us=self.t_on_us, t_off_ms=self.t_off_ms)


def calculate_pwm(frequency_hz, t_on_us, *, min_t_off_ms=MIN_T_OFF, reduce_on_time=False):
    """Round frequency first; floor duty so nominal t_ON never increases.

    Ordinary controls reject unsafe settings. MIDI may shorten t_ON while
    preserving pitch, but cannot shorten a negative available on-time.
    Fractions avoid rounding a duty value upwards at a safety boundary.
    """
    frequency = finite_number(frequency_hz, 'Frequenz')
    validate_t_on(t_on_us)
    pause = finite_number(min_t_off_ms, 'Mindestpause')
    if pause <= 0 or frequency < 1:
        raise SignalValidationError('Frequenz muss mindestens 1 Hz und Mindestpause > 0 sein')
    # Above this frequency there is no room even for the minimum pause.
    if _fraction(frequency_hz) * _fraction(pause) >= 1000:
        raise SignalValidationError('Frequenz lässt keine Pulsbreite mit der Mindestpause zu')
    frequency = int(_fraction(frequency_hz))
    requested_on = _fraction(t_on_us)
    allowed_on = min(Fraction(MAX_T_ON),
                     Fraction(MAX_DUTY_CYCLE * 10_000, frequency),
                     Fraction(1_000_000, frequency) - _fraction(pause) * 1000)
    if not reduce_on_time and requested_on > allowed_on:
        raise SignalValidationError(
            f'Einstellung überschreitet {MAX_T_ON} µs t_ON, {MAX_DUTY_CYCLE}% Duty '
            f'oder unterschreitet {pause:g} ms t_OFF')
    duty = int(min(requested_on, allowed_on) * frequency)
    if duty < 1:
        raise SignalValidationError('Pulsbreite ist für diese Frequenz nicht darstellbar')
    return PWMSettings(frequency, duty)


def calculate_ton_toff(t_on_us, t_off_ms):
    validate_t_on(t_on_us)
    off = finite_number(t_off_ms, 't_OFF')
    if off < MIN_T_OFF:
        raise SignalValidationError(f't_OFF muss mindestens {MIN_T_OFF} ms betragen')
    frequency = Fraction(1_000_000) / (_fraction(t_on_us) + _fraction(t_off_ms) * 1000)
    # Validate the requested settings too, rather than rounding an unsafe
    # requested duty into an acceptable one.
    if _fraction(t_on_us) * frequency > MAX_DUTY_CYCLE * 10_000:
        raise SignalValidationError(f'Duty Cycle darf höchstens {MAX_DUTY_CYCLE}% betragen')
    return calculate_pwm(frequency, t_on_us)


def calculate_duty_pwm(duty_percent, frequency_hz):
    duty = finite_number(duty_percent, 'Duty Cycle')
    frequency = finite_number(frequency_hz, 'Frequenz')
    if not 0 < duty <= MAX_DUTY_CYCLE:
        raise SignalValidationError(f'Duty Cycle muss > 0 und höchstens {MAX_DUTY_CYCLE}% sein')
    if frequency < 1:
        raise SignalValidationError('Frequenz muss mindestens 1 Hz sein')
    on = _fraction(duty_percent) * 10_000 / _fraction(frequency_hz)
    return calculate_pwm(frequency_hz, on)
