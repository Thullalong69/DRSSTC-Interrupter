import time
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
MAX_T_ON = 125 # 125 us
MIN_T_OFF = 10  # 1 ms
MAX_DUTY_CYCLE = 10 # 10%
MIDI_MAX_T_ON = 100  # Standard auf 200 µs, kann über API angepasst werden
MIDI_NOTE_RATE_LIMIT = 50  # Minimum Zeit zwischen zwei Noten in ms

# Verbindung zum pigpio-Daemon
pi = pigpio.pi()

# Festlegung der GPIO-Pins

# GPIO-Pins definieren
FBSWITCH1_PIN = 5  # GPIO für Feedbackswitch1
SPEAKER_PIN = 11  # GPIO für Feedbackswitch2
READY_LED_PIN = 17 # GPIO für die System Ready LED
SOFTSTART_PIN = 26  # GPIO für den 56-Ohm-Widerstand (Softstart)
FULLPOWER_PIN = 16  # GPIO für den Bypass des Widerstands (Vollbetrieb)
INTERRUPTER_PIN = 18 # GPIO-Pin für das Interrupter-Signal

# Globale Variable für Softstart-Fortschritt
softstart_progress = 0
softstart_active = False
FB_SWITCH_DUR = 100
# GPIO-Pins initialisieren
pi.set_mode(READY_LED_PIN, pigpio.OUTPUT)
pi.set_mode(SOFTSTART_PIN, pigpio.OUTPUT)
pi.set_mode(FULLPOWER_PIN, pigpio.OUTPUT)
pi.set_mode(INTERRUPTER_PIN, pigpio.OUTPUT)  # Interrupter-Signal als Ausgang
pi.set_mode(FBSWITCH1_PIN, pigpio.OUTPUT) # GPIO für Feedbackswitch1
pi.set_mode(SPEAKER_PIN, pigpio.OUTPUT) # GPIO für Feedbackswitch2
pi.write(FBSWITCH1_PIN, 1)  # Initial auf HIGH setzen  # Initial auf LOW setzen
pi.write(READY_LED_PIN, 1) # System ready.LED an
pi.write(SOFTSTART_PIN, 0)
pi.write(FULLPOWER_PIN, 0)
pi.write(INTERRUPTER_PIN, 0)  # Interrupter-Signal auf LOW setzen



# MIDI-Pfad definition
MIDI_FILES_DIR = './data/midi-files/'
current_midi_data = []


# Statusvariable, um zu tracken, ob der Slider bewegt wurde
slider_moved = False

    
@app.route('/')
def index():
    return render_template('index.html')
    pi.write(INTERRUPTER_PIN, 0)

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
        # Schalte das Relais für den 56-Ohm-Widerstand ein
        pi.write(SOFTSTART_PIN, 1)  # RELAIS_PIN_1 auf HIGH setzen
        print("Relais 1 (2,2k -Ohm-Widerstand) aktiviert.")
        time.sleep(20)  # Warte 5 Sekunden für FB_KICK_DURdas Vorladen der Kondensatoren

        # Schalte das Relais für den direkten Betrieb ein und das erste aus


        pi.write(FULLPOWER_PIN, 1)  # RELAIS_PIN_2 auf HIGH setzen
        print("Relais 2 (Direkter Betrieb) aktiviert.")

        # Setze den Fortschritt auf 100%
        softstart_progress = 100
        print("Softstart abgeschlossen.")
    except Exception as e:
        print(f"Fehler während des Softstarts: {e}")
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
    try:
        # Hier kannst du das Abbrechen der MIDI-Wiedergabe hinzufügen
        is_playing = False
        return jsonify({'status': 'success', 'message': 'Wiedergabe gestoppt'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
        #


@app.route('/set_ton_toff', methods=['POST'])
def set_ton_toff():
    global slider_moved
    t_on = request.form.get('t_on', type=int)
    t_off = request.form.get('t_off', type=int)

    if not t_on or not t_off:
        return jsonify({"status": "error", "message": "t_ON oder t_OFF fehlt"}), 400
        
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
        # Setze den Pin für die angegebene Zeit auf HIGH
        pi.gpio_trigger(INTERRUPTER_PIN, t_on, 1)

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
    """
    Handles the MIDI file playback request.
    Expects a 'midi_file' parameter in the form data.
    """
    try:
        midi_file = 0
        filepath = os.path.join(MIDI_FILES_DIR, request.form.get('midi_file', ''))
        global is_playing
        is_playing = True
        play_midi_file(filepath)  # Funktion zum Abspielen der Datei
        return jsonify({'status': 'success', 'message': f'{midi_file} abgespielt'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})
        is_playing = False

def send_pulse(t_on, frequency):
    # Konfiguriere Hardware PWM für den Interrupt-Pin
    pi.hardware_PWM(INTERRUPTER_PIN, frequency, int(t_on * 10000))  # Frequenz und Duty Cycle
    # Verwenden Sie pigpio's Zeitsteuerung anstelle von time.sleep()
    pi.set_watchdog(INTERRUPTER_PIN, t_on)
    pi.write(INTERRUPTER_PIN, 0)  # Schalte das Signal nach der on-Time wieder aus

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def play_midi_file(filepath):
    """ Spielt eine konvertierte .dat-Datei ab, jetzt ohne Jitter und Pitching """
    print(f"Starte Wiedergabe der Datei: {filepath}")
    last_note_time = time.perf_counter_ns()  # Startzeit in Nanosekunden
    active_notes = set()  # Set zum Verfolgen der aktiven Noten

    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(5), b''):  # Jede Zeile ist ein 5-Byte-Block
                dt, ev_type, note, vel = struct.unpack('<HBBB', chunk)
                print(f"Delay: {dt} ms, Event: {ev_type}, Note: {note}, Velocity: {vel}")

                current_time = time.perf_counter_ns()  # aktuelle Zeit in Nanosekunden
                elapsed_time = (current_time - last_note_time) / 1_000_000  # vergangene Zeit in Millisekunden
                if elapsed_time < dt:
                    time.sleep((dt - elapsed_time) / 1000.0)  # Schlafen für die verbleibende Zeit
                last_note_time = time.perf_counter_ns()  # Aktualisiere die letzte Notenzeit

                if ev_type == 0x90:  # NOTE_ON
                    if len(active_notes) < 4:  # Begrenzung auf 4 gleichzeitige Noten
                        frequency = midi_note_to_frequency(note)
                        max_duty_cycle = calculate_max_duty_cycle(frequency, MIDI_MAX_T_ON)
                        print(f"NOTE_ON: {note}, Frequency: {frequency:.2f} Hz, Duty Cycle: {max_duty_cycle / 10_000:.2f}%")
                        pi.hardware_PWM(INTERRUPTER_PIN, int(frequency), max_duty_cycle)
                        active_notes.add(note)
                    else:
                        print(f"Maximale Anzahl gleichzeitiger Noten erreicht. Note {note} wird ignoriert.")
                elif ev_type == 0x80:  # NOTE_OFF
                    print(f"NOTE_OFF: {note}")
                    pi.hardware_PWM(INTERRUPTER_PIN, 0, 0)  # PWM ausschalten
                    active_notes.discard(note)

        print("Wiedergabe abgeschlossen.")
    except Exception as e:
        print(f"Fehler beim Abspielen der Datei: {e}")

def midi_note_to_frequency(note):
    """ Berechnet die Frequenz der MIDI-Note """
    return 440.0 * 2.0 ** ((note - 69) / 12.0)  # Standardmäßige MIDI-Tonhöhenformel

@app.route('/set_midi_max_t_on', methods=['POST'])
def set_midi_max_t_on():
    """ Ermöglicht die Justierung der maximalen On-Time im MIDI-Modus """
    global MIDI_MAX_T_ON
    new_max_t_on = request.form.get('max_t_on', type=int)

    if new_max_t_on is None or new_max_t_on <= 0 or new_max_t_on > MAX_T_ON:
        return jsonify({
            "status": "error",
            "message": f"max_t_on muss zwischen 1 und {MAX_T_ON} µs liegen"
        }), 400

    MIDI_MAX_T_ON = new_max_t_on
    return jsonify({
        "status": "success",
        "message": f"Maximale MIDI On-Time gesetzt auf {MIDI_MAX_T_ON} µs"
    })

def calculate_max_duty_cycle(frequency, max_t_on):
    """ Berechnet den maximalen Duty Cycle in Millionstel basierend auf max_t_on """
    period = 1.0 / frequency  # Periode in Sekunden
    max_on_time = max_t_on / 1_000_000.0  # Umrechnen in Sekunden
    duty_cycle = (max_on_time / period) * 1_000_000  # Umrechnen in Millionstel
    return min(int(duty_cycle), 1_000_000)  # Begrenzen auf maximal 1.000.000 (100%)

@app.route('/playback_status', methods=['GET'])
def playback_status():
    """Überprüft, ob MIDI gerade abgespielt wird"""
    global is_playing
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
        if power_state:
            # Softstart aktivieren
            pi.write(SOFTSTART_PIN, 1)  # 56-Ohm-Widerstand aktivieren
            print("Softstart aktiviert (SOFTSTART_PIN HIGH).")
            time.sleep(20)  # Warte 5 Sekunden

            # Volllast aktivieren und Softstart deaktivieren
            pi.write(FULLPOWER_PIN, 1)  # Volllast aktivieren
            print("325 VDC aktiviert full Mains live")
            
            return jsonify({
                "status": "success", 
                "message": "Softstart abgeschlossen, Volllast aktiviert",
                "power": True
            })
        else:
            # Relais deaktivieren
            pi.write(SOFTSTART_PIN, 0)
            pi.write(FULLPOWER_PIN, 0)
            print("Alle Relais deaktiviert (SOFTSTART_PIN und FULLPOWER_PIN LOW).")
            return jsonify({
                "status": "success",
                "message": "System ausgeschaltet",
                "power": False
            })
    except Exception as e:
        # Fehlerbehandlung und Notabschaltung
        pi.write(SOFTSTART_PIN, 0)
        pi.write(FULLPOWER_PIN, 0)
        print(f"Fehler beim Schalten: {e}")
        return jsonify({
            "status": "error",
            "message": f"Fehler beim Schalten: {str(e)}",
            "power": False
        }), 500
        

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')



