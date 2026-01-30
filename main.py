import time
import subprocess
import math
import struct
import os
import logging
import threading
from matplotlib.transforms import offset_copy
import pigpio
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# Begrenzungen
MAX_T_ON = 200 # 125 us
MIN_T_OFF = 5  # 1 ms
MAX_DUTY_CYCLE = 1 # 10%
MIDI_MAX_T_ON = 100  # Standard auf 200 µs, kann über API angepasst werden
MIDI_NOTE_RATE_LIMIT = 50  # Minimum Zeit zwischen zwei Noten in ms
NOTE_BLOCK_TIME_US = 1000  # Sperrzeit nach jedem Pulse in Mikrosekunden

# Verbindung zum pigpio-Daemon
pi = pigpio.pi()

# Festlegung der GPIO-Pins

# GPIO-Pins definieren
READY_LED_PIN = 16 # GPIO für die System Ready LED
SOFTSTART_PIN = 20  # GPIO für den 56-Ohm-Widerstand (Softstart)
FULLPOWER_PIN = 21  # GPIO für den Bypass des Widerstands (Vollbetrieb)
INTERRUPTER_PIN = 12 # GPIO-Pin für das Interrupter-Signal
SPEAKER_PIN = 13 # GPIO-Pin für akustische Signale
# Globale Variable für Softstart-Fortschritt
connection_ok = False  # Verbindung zu deinem Handy
softstart_progress = 0
softstart_active = False
FORCE_GPIO_TRIGGER = False  # Umschaltbar für Debug / Produktion
is_playing = False  # Statusvariable für MIDI-Wiedergabe
burst_active = False  # Globale Variable für Burst-Modus-Status
cw_running = False
cw_lock = threading.Lock()
power_lock = threading.Lock()

def play_beep(pin, freq=1000, duration_ms=200):
    """
    Gibt einen kurzen Piepton auf dem angegebenen Pin aus.
    """
    pi.set_PWM_frequency(pin, freq)
    pi.set_PWM_dutycycle(pin, 128)  # 50% Duty Cycle
    time.sleep(duration_ms / 1000)
    pi.set_PWM_dutycycle(pin, 0)


# GPIO-Pins initialisieren
pi.set_mode(READY_LED_PIN, pigpio.OUTPUT)
pi.set_mode(SOFTSTART_PIN, pigpio.OUTPUT)
pi.set_mode(FULLPOWER_PIN, pigpio.OUTPUT)
pi.set_mode(INTERRUPTER_PIN, pigpio.OUTPUT)  # Interrupter-Signal als Ausgang
pi.set_mode(SPEAKER_PIN, pigpio.OUTPUT)
pi.write(READY_LED_PIN, 1) # System ready.LED an
# Akustische Bestätigung
play_beep(SPEAKER_PIN, freq=444, duration_ms=200)
pi.write(SOFTSTART_PIN, 1)
pi.write(FULLPOWER_PIN, 1)
pi.write(INTERRUPTER_PIN, 0)  # Interrupter-Signal auf LOW setzen
pi.write(SPEAKER_PIN, 0)

# Logging konfigurieren
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(message)s')
logger = logging.getLogger("MIDI")

# MIDI-Pfad definition
MIDI_FILES_DIR = './data/midi-files/'
current_midi_data = []

def _stop_all_outputs():
    """
    Stoppt alle pigpio-Ausgaben am INTERRUPTER_PIN und setzt sicher LOW.
    Idempotent, kann jederzeit aufgerufen werden.
    """
    try:
        pi.wave_tx_stop()
    except Exception:
        pass
    try:
        pi.wave_clear()
    except Exception:
        pass
    # Hardware-PWM sicher aus
    pi.hardware_PWM(INTERRUPTER_PIN, 0, 0)
    # Software-PWM sicher aus
    try:
        pi.set_PWM_dutycycle(INTERRUPTER_PIN, 0)
    except Exception:
        pass
    # Pin Low
    pi.write(INTERRUPTER_PIN, 0)

def safe_power_off(reason=None):
    """
    Fail-safe: Schaltet beide Relais zuverlässig ab (active low -> HIGH = AUS).
    """
    pi.write(SOFTSTART_PIN, 1)
    pi.write(FULLPOWER_PIN, 1)
    if reason:
        print(f"[Power-Off] {reason}")

@app.route('/start_cw', methods=['POST'])
def start_cw():
    """
    Minimaler CW-Start: alle anderen Outputs stoppen, Interrupter-Pin dauerhaft HIGH.
    """
    global cw_running, is_playing, burst_active
    with cw_lock:
        if cw_running:
            # idempotent – Frontend bekommt 'läuft schon'
            return jsonify({'status': 'success', 'message': 'CW läuft bereits'})
        # Andere Modi sauber beenden
        is_playing = False
        burst_active = False
        _stop_all_outputs()

        # Dauerhaftes Enable: Interrupter auf HIGH
        pi.write(INTERRUPTER_PIN, 1)
        cw_running = True
    return jsonify({'status': 'success', 'message': 'CW gestartet'})


@app.route('/stop_cw', methods=['POST'])
def stop_cw():
    """
    CW stoppen: Outputs killen & Pin LOW.
    """
    global cw_running
    with cw_lock:
        if not cw_running:
            _stop_all_outputs()  # idempotent
            return jsonify({'status': 'success', 'message': 'CW war nicht aktiv'})
        cw_running = False
        _stop_all_outputs()
    return jsonify({'status': 'success', 'message': 'CW gestoppt'})

    
@app.route('/')
def index():
    return render_template('index.html')
    pi.write(INTERRUPTER_PIN, 0)

def ping_device(ip):
    try:
        output = subprocess.run(['ping', '-c', '1', '-W', '1', ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output.returncode == 0
    except Exception:
        return False

def watchdog():
    global connection_ok
    handy_ip = "192.168.178.86"  # DEINE Handy-IP hier eintragen!
    while True:
        connection_ok = ping_device(handy_ip)
        print(f"[Watchdog] Verbindung zu Handy ({handy_ip}): {'OK' if connection_ok else 'FEHLT'}")
        time.sleep(2)  # alle 2 Sekunden checken

# Starte den Watchdog beim Boot
threading.Thread(target=watchdog, daemon=True).start()

def send_precise_pulse(pin, t_on_us):
    """
    Erzeugt einen einzelnen HIGH-Puls mit exakter Länge (t_on_us) auf dem angegebenen Pin.
    Nutzt pigpio Wave-API für Pulse >100 µs.
    """
    pi.wave_clear()
    pulses = [
        pigpio.pulse(1 << pin, 0, t_on_us),   # Pin HIGH für t_on_us
        pigpio.pulse(0, 1 << pin, 1)          # Pin LOW danach (1 µs, damit sauber zurückgesetzt)
    ]
    pi.wave_add_generic(pulses)
    wid = pi.wave_create()
    if wid >= 0:
        pi.wave_send_once(wid)
        while pi.wave_tx_busy():
            pass
        pi.wave_delete(wid)

def set_pwm(t_on_us, t_off_ms):
    if t_on_us > MAX_T_ON:
        t_on_us = MAX_T_ON  # Begrenze t_ON auf 100 µs
        print(f"Warnung: t_ON darf nicht größer als {MAX_T_ON} µs sein, setze auf {MAX_T_ON} µs")

    if t_off_ms < MIN_T_OFF:
        return jsonify({"status": "error", "message": f"t_OFF darf nicht kleiner als {MIN_T_OFF} ms sein"}), 400

    t_total_ms = t_on_us / 1_000 + t_off_ms  # Gesamtzeit in Millisekunden
    frequency = 1_000 / t_total_ms  # Frequenz in Hertz (1 kHz / Gesamtzeit in Millisekunden)
    duty_cycle = (t_on_us / 1_000) / t_total_ms * 1_000_000  # Duty Cycle in Prozent

    print(f"Setze PWM: Frequenz = {frequency} Hz, Duty Cycle = {duty_cycle} (von 1.000.000)")

    # Setze die Hardware-PWM-Frequenz und den Duty Cycle
    pi.hardware_PWM(INTERRUPTER_PIN, int(frequency), int(duty_cycle))  # Duty Cycle in Millionstel von 1

@app.route('/set_burst', methods=['POST'])
def set_burst():
    """
    Setzt BPS (Bursts pro Sekunde) und t_ON (in Mikrosekunden) für den Burst-Modus.
    Berechnet daraus die Periodendauer und startet eine Interrupter-Sequenz.
    Achtet auf MAX_T_ON, MIN_T_OFF und MAX_DUTY_CYCLE.
    """
    global burst_active
    try:
        bps = float(request.form.get('bps', 0))
        t_on = int(request.form.get('t_on', 0))

        if t_on == 0:
            _stop_all_outputs()
            burst_active = False
            return jsonify({
                "status": "success",
                "message": "Burst-Modus deaktiviert (t_ON = 0 µs)",
                "active": False
            })

        if bps <= 0:
            return jsonify({"status": "error", "message": "bps muss > 0 sein"}), 400

        if t_on < 0 or t_on > MAX_T_ON:
            return jsonify({
                "status": "error",
                "message": f"t_ON muss zwischen 1 und {MAX_T_ON} µs liegen"
            }), 400

        period_ms = 1000 / bps
        t_off_ms = period_ms - (t_on / 1000)  # in ms

        if t_off_ms < MIN_T_OFF:
            return jsonify({
                "status": "error",
                "message": f"t_OFF ist zu klein (< {MIN_T_OFF} ms). Wähle kleinere BPS oder t_ON."
            }), 400

        # Duty Cycle in Prozent berechnen
        duty_percent = (t_on / 1000) / period_ms * 100
        if duty_percent > MAX_DUTY_CYCLE:
            return jsonify({
                "status": "error",
                "message": f"Duty Cycle zu hoch: {duty_percent:.2f}%. Max erlaubt: {MAX_DUTY_CYCLE}%"
            }), 400

        frequency = int(1000 / period_ms)
        duty_cycle = int((t_on / 1000) / period_ms * 1_000_000)  # für pigpio

        pi.hardware_PWM(INTERRUPTER_PIN, frequency, duty_cycle)
        burst_active = True

        return jsonify({
            "status": "success",
            "message": f"Burst-Modus aktiv: {bps} BPS, t_ON: {t_on} µs, t_OFF: {t_off_ms:.2f} ms, Duty: {duty_percent:.2f}%",
            "active": True
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Fehler bei der Verarbeitung: {str(e)}"
        }), 500
    


@app.route('/burst_status', methods=['GET'])
def burst_status():
    """
    Gibt den aktuellen Status des Burst-Modus zurück.
    """
    global burst_active
    return jsonify({
        "active": burst_active
    })




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



def softstart_sequence():
    global softstart_progress, softstart_active
    softstart_active = True
    softstart_progress = 0

    print("Softstart gestartet...")

    try:
        with power_lock:
            # Schalte das Relais für den 56-Ohm-Widerstand ein (active low)
            pi.write(SOFTSTART_PIN, 0)
            print("Relais 1 (Softstart) aktiviert.")
            time.sleep(20)  # Warte 20 Sekunden für das Vorladen der Kondensatoren

            # Schalte das Relais für den direkten Betrieb ein und Softstart aus
            pi.write(FULLPOWER_PIN, 0)
            pi.write(SOFTSTART_PIN, 1)
            print("Relais 2 (Direkter Betrieb) aktiviert.")

        # Setze den Fortschritt auf 100%
        softstart_progress = 100
        print("Softstart abgeschlossen.")
    except Exception as e:
        safe_power_off(f"Fehler während des Softstarts: {e}")
    finally:
        softstart_active = False
    
@app.route('/start_softstart', methods=['POST'])
def start_softstart():
    """
    Startet den Softstart in einem separaten Thread.
    """
    global softstart_active

    if softstart_active:
        return jsonify({"status": "error", "message": "Softstart läuft bereits"}), 400

    threading.Thread(target=softstart_sequence).start()
    return jsonify({"status": "Softstart gestartet"})


@app.route('/stop_midi', methods=['POST'])
def stop_midi():
    global is_playing
    is_playing = False
    pi.hardware_PWM(INTERRUPTER_PIN, 0, 0)
    return jsonify({'status': 'success', 'message': 'Wiedergabe gestoppt'})


@app.route('/set_ton_toff', methods=['POST'])
def set_ton_toff():
    t_on = request.form.get('t_on', type=int)
    t_off = request.form.get('t_off', type=int)

    if t_on is None or t_off is None:
        return jsonify({"status": "error", "message": "t_ON oder t_OFF fehlt"}), 400
    if t_on == 0:
        _stop_all_outputs()
        return jsonify({"status": "success", "message": "t_ON = 0 µs, Interrupter deaktiviert"})
    if t_on < 0 or t_off <= 0:
        return jsonify({"status": "error", "message": "t_ON oder t_OFF ungültig"}), 400
    
        
    # PWM setzen
    t_total_ms = t_on / 1_000 + t_off  # Gesamtzeit in Millisekunden
    frequency = 1_000 / t_total_ms  # Frequenz in Hertz
    duty_cycle = (t_on / 1_000) / t_total_ms * 1_000_000  # Duty Cycle
    pi.hardware_PWM(INTERRUPTER_PIN, int(frequency), int(duty_cycle))
    
    return jsonify({"status": "success", "message": f"t_ON: {t_on} µs, t_OFF: {t_off} µs"})

@app.route('/set_duty_cycle', methods=['POST'])
def set_duty_cycle():
    duty_cycle = request.form.get('duty_cycle', type=float)  # Duty Cycle in Prozent
    frequency = request.form.get('frequency', type=int)  # Frequenz in Hz

    if duty_cycle is None or frequency is None:
        return jsonify({"status": "error", "message": "Ungültige Eingabedaten"}), 400
    if duty_cycle <= 0:
        _stop_all_outputs()
        return jsonify({"status": "success", "message": "Duty Cycle = 0%, Interrupter deaktiviert"}), 200

    # Berechne die on-time in Mikrosekunden
    period_us = (1 / frequency) * 1_000_000  # Periode in Mikrosekunden
    t_on_us = (duty_cycle / 100) * period_us  # on-time in Mikrosekunden

    # Überprüfen, ob die on-time die Grenze überschreitet
    if t_on_us > MAX_T_ON:
        return jsonify({"status": "error", "message": f"Duty Cycle ergibt eine t_ON von {t_on_us:.2f} µs, das Maximum ist {MAX_T_ON} µs"}), 400

    # Setze die PWM entsprechend
    duty_cycle_million = int(duty_cycle * 10_000)  # Umrechnung für pigpio (0 - 1 Million)
    pi.hardware_PWM(INTERRUPTER_PIN, frequency, duty_cycle_million)

    return jsonify({"status": "success", "message": f"Duty Cycle = {duty_cycle}%, Frequenz = {frequency} Hz, t_ON = {t_on_us:.2f} µs"}), 200

@app.route('/single_shot', methods=['POST'])
def single_shot():
    # Stellen Sie sicher, dass der Pin initial LOW ist
    pi.write(INTERRUPTER_PIN, 0)
    t_on = request.form.get('t_on', type=int)  # t_ON in µs
    print(f"t_ON: {t_on} empfangen")

    # Überprüfung der Eingabe
    if t_on is None or t_on <= 0 or t_on > MAX_T_ON:
        return jsonify({
            "status": "error",
            "message": f"t_ON muss zwischen 1 und {MAX_T_ON} µs liegen"
        }), 400

    try:
        if t_on <= 100:
            pi.gpio_trigger(INTERRUPTER_PIN, t_on, 1)
        else:
            send_precise_pulse(INTERRUPTER_PIN, t_on)

        return jsonify({
            "status": "success",
            "message": f"Single Shot mit t_ON = {t_on} µs gefeuert"
        })

    except Exception as e:
        pi.write(INTERRUPTER_PIN, 0)  # Sicherstellen, dass der Pin LOW ist
        return jsonify({
            "status": "error",
            "message": f"Fehler beim Ausführen des Single Shot: {str(e)}"
        }), 500

# Stellen Sie sicher, dass der Pin initial LOW ist

pi.write(INTERRUPTER_PIN, 0)

@app.route('/play_midi', methods=['POST'])
def play_midi():
    global is_playing
    if is_playing:
        return jsonify({'status': 'error', 'message': 'Wiedergabe läuft bereits'})
    try:
        filepath = os.path.join(MIDI_FILES_DIR, request.form.get('midi_file', ''))
        is_playing = True
        threading.Thread(target=play_midi_file, args=(filepath,), daemon=True).start()
        return jsonify({'status': 'success', 'message': 'Wiedergabe gestartet'})
    except Exception as e:
        is_playing = False
        return jsonify({'status': 'error', 'message': str(e)})


def send_pulse(t_on, frequency):
    # Konfiguriere Hardware PWM für den Interrupt-Pin
    pi.hardware_PWM(INTERRUPTER_PIN, frequency, int(t_on * 10000))  # Frequenz und Duty Cycle
    # Verwenden Sie pigpio's Zeitsteuerung anstelle von time.sleep()
    pi.set_watchdog(INTERRUPTER_PIN, t_on)
    pi.write(INTERRUPTER_PIN, 0)  # Schalte das Signal nach der on-Time wieder aus

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def play_midi_file(filepath):
    global is_playing
    logger.info(f"Starte Wiedergabe der Datei: {filepath}")
    last_note_time = time.perf_counter_ns()
    last_trigger_time = 0
    active_note = None  # Monophone Mode
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(5), b''):
                if not is_playing:
                    break
                dt, ev_type, note, vel = struct.unpack('<HBBB', chunk)
                target_time = last_note_time + int(dt * 1_000_000)
                while time.perf_counter_ns() < target_time:
                    pass
                last_note_time = time.perf_counter_ns()

                timestamp = time.strftime("%H:%M:%S", time.localtime())

                if ev_type == 0x90:
                    logger.info(f"[{timestamp}] NOTE_ON: {note}, Velocity: {vel}")

                    now_ns = time.perf_counter_ns()
                    if now_ns - last_trigger_time < NOTE_BLOCK_TIME_US * 1000:
                        logger.info(f"{note} geblockt durch Hard-Off-Time")
                        continue

                    active_note = note
                    if FORCE_GPIO_TRIGGER:
                        pi.gpio_trigger(INTERRUPTER_PIN, MIDI_MAX_T_ON, 1)
                    else:
                        freq = midi_note_to_frequency(note)
                        period = 1.0 / freq
                        max_on_time_s = MIDI_MAX_T_ON / 1_000_000.0
                        duty = calculate_max_duty_cycle(freq, MIDI_MAX_T_ON)

                        actual_on_time = duty / 1_000_000 * period
                        if actual_on_time > max_on_time_s:
                            duty = int((max_on_time_s / period) * 1_000_000)
                            logger.info(f"t_ON begrenzt auf {max_on_time_s * 1e6:.1f} µs bei {freq:.1f} Hz")

                        pi.hardware_PWM(INTERRUPTER_PIN, int(freq), duty)
                    last_trigger_time = time.perf_counter_ns()

                elif ev_type == 0x80 and note == active_note:
                    logger.info(f"[{timestamp}] NOTE_OFF: {note}")
                    pi.hardware_PWM(INTERRUPTER_PIN, 0, 0)
                    active_note = None

    except Exception as e:
        logger.error(f"Fehler beim Abspielen der Datei: {e}")
    finally:
        is_playing = False
        pi.hardware_PWM(INTERRUPTER_PIN, 0, 0)
        logger.info("Wiedergabe abgeschlossen oder abgebrochen.")


def midi_note_to_frequency(note):
    """ Berechnet die Frequenz der MIDI-Note """
    return 440.0 * 2.0 ** ((note - 69) / 12.0)  # Standardmäßige MIDI-Tonhöhenformel

@app.route('/set_midi_max_t_on', methods=['POST'])
def set_midi_max_t_on():
    global MIDI_MAX_T_ON
    new_ton = request.form.get('max_t_on', type=int)
    if new_ton is None or new_ton <= 0 or new_ton > MAX_T_ON:
        return jsonify({'status': 'error', 'message': f"max_t_on muss zwischen 1 und {MAX_T_ON} µs liegen"}), 400
    MIDI_MAX_T_ON = new_ton
    return jsonify({'status': 'success', 'message': f"max_t_on auf {MIDI_MAX_T_ON} µs gesetzt"})

def calculate_max_duty_cycle(freq, max_t_on):
    period = 1.0 / freq
    on_time = max_t_on / 1_000_000.0
    duty = (on_time / period) * 1_000_000
    return min(int(duty), 1_000_000)

@app.route('/playback_status', methods=['GET'])
def playback_status():
    return jsonify({'playing': is_playing})

@app.route('/get_midi_files', methods=['GET'])
def get_midi_files():
    # Lese die Dateien im MIDI-Ordner
    midi_files = [f for f in os.listdir(MIDI_FILES_DIR) if os.path.isfile(os.path.join(MIDI_FILES_DIR, f))]

    # Entferne eventuell vorhandene Dateiendungen (z.B. .dat)
    midi_files = [os.path.splitext(f)[0] for f in midi_files]

    return jsonify({'files': midi_files})


@app.route('/toggle_power', methods=['POST'])
def toggle_power():
    """
    Schaltet die Relais für Softstart und Volllast ein und aus.
    """
    data = request.get_json()
    power_state = data.get('power', False)
    
    try:
        with power_lock:
            if power_state:
                # Softstart aktivieren (active low)
                pi.write(SOFTSTART_PIN, 0)
                print("Softstart aktiviert (SOFTSTART_PIN LOW).")
                time.sleep(20)  # Warte 20 Sekunden

                # Volllast aktivieren und Softstart deaktivieren
                pi.write(FULLPOWER_PIN, 0)
                pi.write(SOFTSTART_PIN, 1)
                print("325 VDC aktiviert full Mains live")

                # Akustische Bestätigung
                play_beep(SPEAKER_PIN, freq=784, duration_ms=1000)   # G5

                return jsonify({
                    "status": "success",
                    "message": "Softstart abgeschlossen, Volllast aktiviert",
                    "power": True
                })

            safe_power_off("Power aus per API")
            print("Alle Relais deaktiviert (SOFTSTART_PIN und FULLPOWER_PIN HIGH).")
            return jsonify({
                "status": "success",
                "message": "System ausgeschaltet",
                "power": False
            })
    except Exception as e:
        # Fehlerbehandlung und Notabschaltung
        safe_power_off(f"Fehler beim Schalten: {e}")
        return jsonify({
            "status": "error",
            "message": f"Fehler beim Schalten: {str(e)}",
            "power": False
        }), 500
        

@app.route('/ping_status', methods=['GET'])
def ping_status():
    handy_ip = "192.168.178.86"  # Deine Handy-IP
    try:
        start = time.time()
        output = subprocess.run(['ping', '-c', '1', '-W', '1', handy_ip], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        duration = int((time.time() - start) * 1000)  # in ms
        ok = output.returncode == 0
    except Exception:
        ok = False
        duration = None
    return jsonify({"connection_ok": ok, "ping_ms": duration})


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
