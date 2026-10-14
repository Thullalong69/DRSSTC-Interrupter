import time
import io
import struct
import os
import logging
import threading
from functools import wraps
from pathlib import Path
from uuid import UUID
import pigpio
from flask import Flask, request, jsonify, render_template, session
from werkzeug.exceptions import HTTPException
from signal_limits import (
    MAX_T_ON, MIN_T_OFF, MAX_DUTY_CYCLE, SignalValidationError,
    PulseRateLimitError, finite_number, validate_t_on, calculate_pwm,
    calculate_ton_toff, calculate_duty_pwm,
)
from output_controller import OutputController, OutputHardwareError, ModeConflictError
from control_lease import ControlLease, LeaseConflictError
from credentials import load_credentials
from midi_library import load_midi, list_midi, validate_records
from process_lock import ProcessLock
from security import install_security

app = Flask(__name__)

# Begrenzungen
MIDI_MAX_T_ON = 100  # Mikrosekunden; die tatsächliche Pulsbreite kann kleiner sein
MIDI_MIN_T_OFF = MIN_T_OFF  # Keine kürzere Pause ohne bestätigte Hardwaregrenzen

# Verbindung zum pigpio-Daemon
pi = None  # Importing the web application never connects to physical hardware.
runtime_owner = None
runtime_stop = threading.Event()
runtime_thread = None
runtime_ready = False

# Festlegung der GPIO-Pins

# GPIO-Pins definieren
READY_LED_PIN = 16 # GPIO für die System Ready LED
SOFTSTART_PIN = 20  # GPIO für den 56-Ohm-Widerstand (Softstart)
FULLPOWER_PIN = 21  # GPIO für den Bypass des Widerstands (Vollbetrieb)
INTERRUPTER_PIN = 12 # GPIO-Pin für das Interrupter-Signal
SPEAKER_PIN = 13 # GPIO-Pin für akustische Signale
# Globale Variable für Softstart-Fortschritt
softstart_progress = 0
softstart_active = False
FORCE_GPIO_TRIGGER = False  # Umschaltbar für Debug / Produktion
MIDI_PITCH = 0
power_lock = threading.RLock()
SOFTSTART_SECONDS = 20
power_cancel = None
last_pulse_finished_at = None
last_pulse_t_on = 0

@app.errorhandler(SignalValidationError)
def invalid_signal(error):
    code = 429 if isinstance(error, PulseRateLimitError) else 400
    return jsonify({'status': 'error', 'message': str(error)}), code

@app.errorhandler(OutputHardwareError)
def output_hardware_error(error):
    if not (app.testing and app.config['AUTH_DISABLED']):
        with control_lease.lock:
            if output_controller.status()['mode'] == 'fault':
                try:
                    control_lease.power_off('Ausgangsfehler')
                except Exception:
                    logger.exception('Abschaltung nach Ausgangsfehler fehlgeschlagen')
    return jsonify({'status': 'error', 'message': str(error)}), 500

@app.errorhandler(ModeConflictError)
def mode_conflict(error):
    return jsonify({'status': 'error', 'message': str(error)}), 409

@app.errorhandler(LeaseConflictError)
def lease_conflict(error):
    return jsonify(status='error', message=str(error)), 409

@app.errorhandler(Exception)
def unexpected_error(error):
    if isinstance(error, HTTPException):
        return jsonify(status='error', message=error.description), error.code
    logger.exception('Unerwarteter Anwendungsfehler')
    if request.method == 'POST' and pi is not None:
        try:
            control_lease.power_off('Unerwarteter Anwendungsfehler')
        except Exception:
            logger.exception('Notabschaltung fehlgeschlagen')
    return jsonify(status='error', message='Interner Fehler; Hardwarezustand prüfen'), 500

@app.after_request
def include_output_state(response):
    if (request.method == 'POST' and response.is_json
        and (session.get('authenticated') or app.testing and app.config['AUTH_DISABLED'])):
        data = response.get_json()
        if isinstance(data, dict):
            data['output'] = output_controller.status()
            response.set_data(app.json.dumps(data))
    return response

# Logging konfigurieren
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("MIDI")

# MIDI-Pfad definition
MIDI_FILES_DIR = Path(__file__).resolve().parent / 'data' / 'midi-files'

def _stop_all_outputs(*, preserve_fault=False):
    output_controller.stop(preserve_fault=preserve_fault)

def _stop_gpio_outputs():
    """
    Stoppt alle pigpio-Ausgaben am INTERRUPTER_PIN und setzt sicher LOW.
    Idempotent, kann jederzeit aufgerufen werden.
    """
    errors = []
    # Jeden Abschaltweg versuchen, auch wenn ein anderer GPIO-Aufruf fehlschlägt.
    for operation in (
        pi.wave_tx_stop, pi.wave_clear,
        lambda: pi.hardware_PWM(INTERRUPTER_PIN, 0, 0),
        lambda: pi.set_PWM_dutycycle(INTERRUPTER_PIN, 0),
        lambda: pi.write(INTERRUPTER_PIN, 0),
    ):
        try:
            operation()
        except Exception as exc:
            errors.append(exc)
            logger.exception('Fehler beim Abschalten des Interrupter-Ausgangs')
    if errors:
        raise errors[0]

def safe_power_off(reason=None, *, preserve_fault=False):
    """
    Fail-safe: Schaltet beide Relais zuverlässig ab (active low -> HIGH = AUS).
    """
    global power_cancel, softstart_active, softstart_progress
    with power_lock:
        if power_cancel is not None:
            power_cancel.set()
        power_cancel = None
        softstart_active = False
        softstart_progress = 0
        # Beide Abschaltversuche ausführen, auch wenn ein GPIO-Aufruf fehlschlägt.
        errors = []
        for pin in (FULLPOWER_PIN, SOFTSTART_PIN):
            try:
                pi.write(pin, 1)
            except Exception as exc:
                errors.append(exc)
                logger.exception("Relais GPIO %s konnte nicht abgeschaltet werden", pin)
        try:
            _stop_all_outputs(preserve_fault=preserve_fault)
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]
    if reason:
        print(f"[Power-Off] {reason}")

def get_power_state():
    """
    Liefert den aktuellen Power-Status basierend auf Relaiszuständen (active low).
    """
    with power_lock:
        if pi is None:
            return False
        softstart_on = pi.read(SOFTSTART_PIN) == 0
        fullpower_on = pi.read(FULLPOWER_PIN) == 0
        return softstart_on or fullpower_on

@app.route('/')
def index():
    return render_template('index.html', max_t_on=MAX_T_ON, min_t_off=MIN_T_OFF,
                           max_duty_cycle=MAX_DUTY_CYCLE, midi_max_t_on=MIDI_MAX_T_ON,
                           midi_pitch=MIDI_PITCH, csrf_token=session.get('csrf_token', ''),
                           preview=app.config.get('READ_ONLY_PREVIEW', False))

def send_precise_pulse(pin, t_on_us):
    """
    Erzeugt einen einzelnen HIGH-Puls mit exakter Länge (t_on_us) auf dem angegebenen Pin.
    Nutzt pigpio Wave-API für Pulse >100 µs.
    """
    t_on = validate_t_on(t_on_us)
    if not t_on.is_integer():
        raise SignalValidationError('Einzelpulse benötigen eine ganzzahlige Pulsbreite in µs')
    t_on_us = int(t_on)
    pi.wave_clear()
    pulses = [
        pigpio.pulse(1 << pin, 0, t_on_us),   # Pin HIGH für t_on_us
        pigpio.pulse(0, 1 << pin, 1)          # Pin LOW danach (1 µs, damit sauber zurückgesetzt)
    ]
    pi.wave_add_generic(pulses)
    wid = pi.wave_create()
    if wid < 0:
        raise RuntimeError('Puls-Wave konnte nicht erstellt werden')
    try:
        pi.wave_send_once(wid)
        deadline = time.monotonic() + 0.5
        while pi.wave_tx_busy():
            if time.monotonic() >= deadline:
                pi.wave_tx_stop()
                pi.write(pin, 0)
                raise RuntimeError('Zeitüberschreitung bei Puls-Wave')
            time.sleep(0.0001)
    finally:
        pi.wave_delete(wid)

def _fire_pulse_locked(t_on_us):
    """Einzelpulse begrenzen, einschließlich wiederholter API-Aufrufe."""
    global last_pulse_finished_at, last_pulse_t_on
    t_on = validate_t_on(t_on_us)
    if not t_on.is_integer():
        raise SignalValidationError('Einzelpulse benötigen eine ganzzahlige Pulsbreite in µs')
    now = time.monotonic()
    # Konservativ ab dem Ende des vorherigen GPIO-Aufrufs messen. Auch beim
    # Wechsel der Pulsbreite darf eine Folge von Einzelpulsen 1% nicht umgehen.
    pause = max(MIN_T_OFF / 1000,
                max(last_pulse_t_on, t_on) / (MAX_DUTY_CYCLE * 10_000))
    if last_pulse_finished_at is not None and now - last_pulse_finished_at < pause:
        raise PulseRateLimitError('Puls zu früh: Mindestpause oder Duty-Cycle-Grenze erreicht')
    try:
        pi.write(INTERRUPTER_PIN, 0)
        if t_on <= 100:
            pi.gpio_trigger(INTERRUPTER_PIN, int(t_on), 1)
        else:
            send_precise_pulse(INTERRUPTER_PIN, int(t_on))
    finally:
        last_pulse_finished_at = time.monotonic()
        last_pulse_t_on = t_on

def set_pwm(t_on_us, t_off_ms):
    settings = calculate_ton_toff(t_on_us, t_off_ms)
    output_controller.start_pwm('ton_toff', settings)
    return settings

def create_output_controller():
    return OutputController(
        stop_outputs=_stop_gpio_outputs,
        apply_pwm=lambda settings: pi.hardware_PWM(INTERRUPTER_PIN, settings.frequency_hz, settings.duty_million),
        fire_pulse=_fire_pulse_locked,
        logger=logger,
    )

output_controller = create_output_controller()
control_lease = ControlLease(lambda reason: safe_power_off(reason, preserve_fault=True))

def operator_identity():
    operator = session.get('operator_id')
    tab = request.headers.get('X-Control-Tab')
    if not operator or not tab:
        return None
    try:
        return operator, UUID(tab).hex
    except (ValueError, AttributeError):
        return None

install_security(app, lambda operator: control_lease.logout(operator_identity()))

def control_command(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        if app.testing and app.config['AUTH_DISABLED']:
            return function(*args, **kwargs)
        if not runtime_ready:
            return jsonify(status='error', message='Hardware ist nicht bereit'), 503
        data = request.get_json(silent=True) if request.is_json else None
        stopping = (function.__name__ in ('stop_output', 'stop_midi')
                    or function.__name__ == 'toggle_power' and isinstance(data, dict) and data.get('power') is False
                    or request.form.get('t_on') == '0' or request.form.get('duty_cycle') == '0')
        if stopping:
            with control_lease.lock:
                if not runtime_ready:
                    return jsonify(status='error', message='Hardware ist nicht bereit'), 503
                return function(*args, **kwargs)
        with control_lease.command(operator_identity()):
            if not runtime_ready:
                return jsonify(status='error', message='Hardware ist nicht bereit'), 503
            return function(*args, **kwargs)
    return guarded

@app.route('/set_burst', methods=['POST'])
@control_command
def set_burst():
    """
    Setzt BPS (Bursts pro Sekunde) und t_ON (in Mikrosekunden) für den Burst-Modus.
    Berechnet daraus die Periodendauer und startet eine Interrupter-Sequenz.
    Achtet auf MAX_T_ON, MIN_T_OFF und MAX_DUTY_CYCLE.
    """
    t_on = validate_t_on(request.form.get('t_on'), allow_zero=True)
    if t_on == 0:
        _stop_all_outputs()
        return jsonify({
            "status": "success",
            "message": "Burst-Modus deaktiviert (t_ON = 0 µs)",
            "active": False
        })
    settings = calculate_pwm(request.form.get('bps'), t_on)
    output_controller.start_pwm('burst', settings)
    return jsonify({"status": "success", "active": True, "settings": settings.as_dict(),
                    "message": f"Burst aktiv: {settings.frequency_hz} BPS, "
                    f"t_ON {settings.t_on_us:g} µs, t_OFF {settings.t_off_ms:.3f} ms, "
                    f"Duty {settings.duty_percent:g}%"})
    


@app.route('/burst_status', methods=['GET'])
def burst_status():
    """
    Gibt den aktuellen Status des Burst-Modus zurück.
    """
    return jsonify({
        "active": output_controller.status()['burst_active']
    })

@app.route('/output_status', methods=['GET'])
def output_status():
    return jsonify(output_controller.status())

@app.route('/stop_output', methods=['POST'])
@control_command
def stop_output():
    _stop_all_outputs()
    return jsonify({'status': 'success', 'message': 'Interrupter-Ausgang gestoppt'})




@app.route('/softstart_status', methods=['GET'])
def softstart_status():
    """
    Gibt den aktuellen Fortschritt des Softstarts zurück.
    """
    global softstart_progress, softstart_active
    return jsonify({
        "active": softstart_active,
        "progress": softstart_progress
    })



def softstart_sequence(cancel):
    """Nur der aktuelle Einschaltauftrag darf auf Volllast umschalten."""
    global softstart_progress, softstart_active
    try:
        started = time.monotonic()
        # Während der Ladezeit keine Sperre halten: AUS muss sofort möglich sein.
        while True:
            remaining = SOFTSTART_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                break
            if cancel.wait(min(remaining, 0.25)):
                return
            with power_lock:
                if power_cancel is not cancel or cancel.is_set():
                    return
                softstart_progress = min(99, int(
                    (time.monotonic() - started) / SOFTSTART_SECONDS * 100))
        with power_lock:
            if power_cancel is not cancel or cancel.is_set():
                return
            pi.write(FULLPOWER_PIN, 0)
            pi.write(SOFTSTART_PIN, 1)
            softstart_progress = 100
            softstart_active = False
    except Exception:
        logger.exception("Fehler während des Softstarts")
        with power_lock:
            # Ein alter Auftrag darf einen neuen Auftrag nicht abschalten.
            if power_cancel is cancel:
                safe_power_off("Fehler während des Softstarts")
    finally:
        with power_lock:
            if power_cancel is cancel:
                softstart_active = False

def begin_softstart():
    """Reserviert einen Einschaltauftrag atomar; wiederholtes EIN ist idempotent."""
    global power_cancel, softstart_active, softstart_progress
    with power_lock:
        if softstart_active:
            return "Softstart läuft bereits"
        if get_power_state():
            return "System ist bereits eingeschaltet"
        cancel = threading.Event()
        power_cancel = cancel
        softstart_active = True
        softstart_progress = 0
        try:
            _stop_all_outputs()
            pi.write(FULLPOWER_PIN, 1)
            pi.write(SOFTSTART_PIN, 0)
            threading.Thread(target=softstart_sequence, args=(cancel,), daemon=True).start()
        except Exception:
            safe_power_off("Softstart konnte nicht gestartet werden")
            raise
        return "Softstart gestartet"
    
@app.route('/start_softstart', methods=['POST'])
@control_command
def start_softstart():
    """
    Startet den Softstart in einem separaten Thread.
    """
    try:
        message = begin_softstart()
        return jsonify({"status": "success", "message": message})
    except Exception:
        logger.exception("Softstart konnte nicht gestartet werden")
        return jsonify({"status": "error", "message": "Softstart konnte nicht gestartet werden"}), 500


@app.route('/stop_midi', methods=['POST'])
@control_command
def stop_midi():
    stopped = output_controller.stop(expected_mode='midi')
    message = 'Wiedergabe gestoppt' if stopped else 'Keine MIDI-Wiedergabe aktiv'
    return jsonify({'status': 'success', 'message': message})


@app.route('/set_ton_toff', methods=['POST'])
@control_command
def set_ton_toff():
    t_on = validate_t_on(request.form.get('t_on'), allow_zero=True)
    if t_on == 0:
        _stop_all_outputs()
        return jsonify({"status": "success", "message": "t_ON = 0 µs, Interrupter deaktiviert"})
    settings = set_pwm(t_on, request.form.get('t_off'))
    return jsonify({"status": "success", "settings": settings.as_dict(),
                    "message": f"t_ON: {settings.t_on_us:g} µs, t_OFF: {settings.t_off_ms:.3f} ms"})

@app.route('/set_duty_cycle', methods=['POST'])
@control_command
def set_duty_cycle():
    duty_cycle = finite_number(request.form.get('duty_cycle'), 'Duty Cycle')
    if duty_cycle == 0:
        _stop_all_outputs()
        return jsonify({"status": "success", "message": "Duty Cycle = 0%, Interrupter deaktiviert"}), 200

    settings = calculate_duty_pwm(duty_cycle, request.form.get('frequency'))
    output_controller.start_pwm('duty_cycle', settings)
    return jsonify({"status": "success", "settings": settings.as_dict(),
                    "message": f"Duty {settings.duty_percent:g}%, Frequenz {settings.frequency_hz} Hz, "
                    f"t_ON {settings.t_on_us:g} µs, t_OFF {settings.t_off_ms:.3f} ms"})

@app.route('/single_shot', methods=['POST'])
@control_command
def single_shot():
    t_on = validate_t_on(request.form.get('t_on'))
    if not t_on.is_integer():
        raise SignalValidationError('Einzelpulse benötigen eine ganzzahlige Pulsbreite in µs')
    output_controller.single_shot(t_on)
    return jsonify({
        "status": "success",
        "message": f"Single Shot mit t_ON = {t_on} µs gefeuert"
    })

@app.route('/play_midi', methods=['POST'])
@control_command
def play_midi():
    # Validate and retain the exact bytes before disturbing the current mode.
    data = load_midi(MIDI_FILES_DIR, request.form.get('midi_file'))
    with output_controller.lock:
        cancel = output_controller.begin_midi()
        try:
            threading.Thread(target=play_midi_file, args=(data, cancel), daemon=True).start()
        except Exception:
            output_controller.finish_midi(cancel, error=RuntimeError('Wiedergabe konnte nicht gestartet werden'))
            logger.exception("MIDI-Wiedergabe konnte nicht gestartet werden")
            return jsonify({'status': 'error', 'message': 'Wiedergabe konnte nicht gestartet werden'}), 500
    return jsonify({'status': 'success', 'message': 'Wiedergabe gestartet'})


def send_pulse(t_on, frequency):
    settings = calculate_pwm(frequency, t_on)
    output_controller.start_pwm('duty_cycle', settings)

def play_midi_file(filepath, cancel):
    logger.info('MIDI-Wiedergabe gestartet')
    target_time = time.monotonic()
    active_note = None  # Monophone Mode
    playback_error = None
    try:
        data = validate_records(filepath) if isinstance(filepath, bytes) else Path(filepath).read_bytes()
        with io.BytesIO(data) as f:
            for chunk in iter(lambda: f.read(5), b''):
                if cancel.is_set():
                    break
                dt, ev_type, note, vel = struct.unpack('<HBBB', chunk)
                target_time += dt / 1000.0
                if cancel.wait(max(0, target_time - time.monotonic())):
                    break

                with output_controller.lock:
                    # STOP kann unmittelbar nach wait() eingetroffen sein.
                    if not output_controller.owns_midi(cancel):
                        break
                    if ev_type == 0x90 and vel > 0:
                        try:
                            if FORCE_GPIO_TRIGGER:
                                output_controller.midi_pulse(cancel, MIDI_MAX_T_ON)
                            else:
                                freq = midi_note_to_frequency(note + MIDI_PITCH)
                                settings = calculate_pwm(freq, MIDI_MAX_T_ON,
                                    min_t_off_ms=MIDI_MIN_T_OFF, reduce_on_time=True)
                                output_controller.midi_pwm(cancel, settings)
                            active_note = note
                        except SignalValidationError as exc:
                            # Die vorherige Note nicht versehentlich weiterlaufen lassen.
                            output_controller.midi_silence(cancel)
                            active_note = None
                            logger.warning("MIDI-Note %s ausgelassen: %s", note, exc)
                    elif (ev_type == 0x80 or (ev_type == 0x90 and vel == 0)) and note == active_note:
                        output_controller.midi_silence(cancel)
                        active_note = None

    except Exception as e:
        playback_error = e
        logger.error(f"Fehler beim Abspielen der Datei: {e}")
    finally:
        try:
            output_controller.finish_midi(cancel, error=playback_error)
        except OutputHardwareError:
            logger.exception('Fehler beim Beenden der MIDI-Wiedergabe')
        logger.info("Wiedergabe abgeschlossen oder abgebrochen.")


def midi_note_to_frequency(note):
    """ Berechnet die Frequenz der MIDI-Note """
    if not isinstance(note, int) or not 0 <= note <= 127:
        raise SignalValidationError('MIDI-Note muss zwischen 0 und 127 liegen')
    return 440.0 * 2.0 ** ((note - 69) / 12.0)  # Standardmäßige MIDI-Tonhöhenformel

@app.route('/set_midi_max_t_on', methods=['POST'])
@control_command
def set_midi_max_t_on():
    global MIDI_MAX_T_ON
    new_ton = validate_t_on(request.form.get('max_t_on'))
    if not new_ton.is_integer():
        raise SignalValidationError('MIDI t_ON benötigt eine ganzzahlige Pulsbreite in µs')
    with output_controller.lock:
        MIDI_MAX_T_ON = int(new_ton)
        state = output_controller.status()
        if state['mode'] == 'midi' and state['settings'] is not None:
            settings = calculate_pwm(state['settings']['frequency_hz'], MIDI_MAX_T_ON,
                                     min_t_off_ms=MIDI_MIN_T_OFF, reduce_on_time=True)
            output_controller.midi_pwm(output_controller.midi_token, settings)
    return jsonify({'status': 'success', 'message': f"max_t_on auf {MIDI_MAX_T_ON} µs gesetzt"})

def calculate_max_duty_cycle(freq, max_t_on):
    return calculate_pwm(freq, max_t_on, min_t_off_ms=MIDI_MIN_T_OFF,
                         reduce_on_time=True).duty_million

@app.route('/playback_status', methods=['GET'])
def playback_status():
    return jsonify({'playing': output_controller.status()['playing']})

@app.route('/get_midi_files', methods=['GET'])
def get_midi_files():
    return jsonify({'files': list_midi(MIDI_FILES_DIR)})

@app.route('/update_pitch', methods=['POST'])
@control_command
def update_pitch():
    global MIDI_PITCH
    value = finite_number(request.form.get('pitch'), 'Pitch')
    if not value.is_integer() or not -12 <= value <= 12:
        raise SignalValidationError('Pitch muss zwischen -12 und 12 Halbtonschritten liegen')
    with output_controller.lock:
        MIDI_PITCH = int(value)
    return jsonify(status='success', message=f'Pitch auf {MIDI_PITCH} Halbtonschritte gesetzt')


@app.route('/toggle_power', methods=['POST'])
@control_command
def toggle_power():
    """
    Schaltet die Relais für Softstart und Volllast ein und aus.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('power')) is not bool:
        return jsonify({"status": "error", "message": "power muss ein boolescher Wert sein"}), 400
    power_state = data['power']
    
    try:
        with power_lock:
            if power_state:
                message = begin_softstart()
            else:
                control_lease.power_off('Power aus per API')
                message = "System ausgeschaltet"
            return jsonify({
                "status": "success",
                "message": message,
                "power": get_power_state(),
                "softstart_active": softstart_active
            })
    except Exception as e:
        # Fehlerbehandlung und Notabschaltung
        logger.exception("Fehler beim Schalten")
        try:
            safe_power_off(f"Fehler beim Schalten: {e}")
        except Exception:
            logger.exception("Notabschaltung konnte nicht vollständig ausgeführt werden")
        return jsonify({
            "status": "error",
            "message": "Fehler beim Schalten; Hardwarezustand prüfen"
        }), 500

@app.route('/power_status', methods=['GET'])
def power_status():
    return jsonify({"power": get_power_state()})
        

@app.route('/ping_status', methods=['GET'])
def ping_status():
    return jsonify(control_lease.status(operator_identity()))

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    identity = operator_identity()
    if identity is None:
        raise LeaseConflictError('Steuerfensterkennung fehlt')
    return jsonify(control_lease.heartbeat(identity))

@app.route('/health', methods=['GET'])
def health():
    return jsonify(ready=runtime_ready), 200 if runtime_ready else 503

def watchdog():
    while not runtime_stop.wait(0.25):
        try:
            # Background MIDI failures do not pass through Flask's error
            # handler. Handle them here in lease -> power -> output lock order,
            # rather than acquiring the power lock from a MIDI output callback.
            with control_lease.lock:
                if output_controller.status()['mode'] == 'fault' and control_lease.owner is not None:
                    control_lease.power_off('Ausgangsfehler im Hintergrund')
                else:
                    control_lease.check_expired()
        except Exception:
            logger.exception('Heartbeat-Abschaltung fehlgeschlagen')

def configure_auth(path, *, public_origin=None):
    record = load_credentials(path)
    app.config.update(SECRET_KEY=record['secret_key'], PASSWORD_HASH=record['password_hash'],
                      PUBLIC_ORIGIN=public_origin, AUTH_DISABLED=False, TESTING=False)

def initialize_hardware(lock_path):
    global pi, runtime_owner, runtime_thread, runtime_ready
    if runtime_owner is not None:
        raise RuntimeError('Hardware wurde bereits initialisiert')
    if not app.config['PASSWORD_HASH'] and not (app.testing and app.config['AUTH_DISABLED']):
        raise RuntimeError('Anmeldung muss vor dem Hardwarestart konfiguriert werden')
    owner = ProcessLock(lock_path)
    owner.acquire()
    runtime_owner = owner
    try:
        pi = pigpio.pi('127.0.0.1')
        if not pi.connected:
            raise RuntimeError('pigpio-Daemon ist nicht erreichbar')
        # Relays OFF and interrupter LOW before ready indication or any wait.
        for pin, level in ((SOFTSTART_PIN, 1), (FULLPOWER_PIN, 1),
                           (INTERRUPTER_PIN, 0), (SPEAKER_PIN, 0), (READY_LED_PIN, 0)):
            pi.write(pin, level)
            pi.set_mode(pin, pigpio.OUTPUT)
        _stop_all_outputs()
        runtime_stop.clear()
        runtime_ready = True
        runtime_thread = threading.Thread(target=watchdog, daemon=True)
        runtime_thread.start()
        pi.write(READY_LED_PIN, 1)
    except Exception:
        shutdown_hardware()
        raise

def shutdown_hardware():
    global pi, runtime_owner, runtime_ready, runtime_thread
    runtime_ready = False
    runtime_stop.set()
    if pi is not None:
        try:
            control_lease.power_off('Prozess wird beendet')
        except Exception:
            logger.exception('Abschaltung beim Beenden fehlgeschlagen')
        try:
            pi.write(READY_LED_PIN, 0)
        except Exception:
            logger.exception('Ready-LED konnte nicht abgeschaltet werden')
        try:
            pi.stop()
        except Exception:
            logger.exception('pigpio-Verbindung konnte nicht geschlossen werden')
        pi = None
    if runtime_owner is not None:
        runtime_owner.release()
        runtime_owner = None
    if runtime_thread is not None and runtime_thread is not threading.current_thread():
        if runtime_thread.is_alive():
            runtime_thread.join(timeout=1)
        runtime_thread = None


if __name__ == "__main__":
    import sys
    from run import main as run_server
    run_server(application=sys.modules[__name__])
