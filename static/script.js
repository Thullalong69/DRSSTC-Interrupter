'use strict';

const element = id => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
// A fresh identifier per page load: another tab sharing the login cookie must
// never keep an abandoned controller alive.
const controlTabId = window.crypto.randomUUID();
let isPlaying = false;
let outputRevision = -1;
let outputControllerId = null;
const retiredOutputControllers = new Set();
const plot = {data: {datasets: [{data: [{x: 0, y: 0}]}]}};

async function api(url, options = {}) {
    const headers = Object.assign({}, options.headers);
    headers['X-Control-Tab'] = controlTabId;
    if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
        const response = await fetch(url, Object.assign({}, options, {
            headers, credentials: 'same-origin', signal: controller.signal}));
        if (response.status === 401) {
            window.location.assign('/login');
            throw new Error('Anmeldung abgelaufen');
        }
        const data = await response.json();
        if (!response.ok && !data.message) throw new Error('Anfrage fehlgeschlagen');
        return data;
    } finally {
        clearTimeout(timer);
    }
}

function postForm(url, fields) {
    return api(url, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams(fields).toString()});
}

function showSignalResult(data) {
    if (data.output && renderOutputState(data.output) === false) return;
    const error = data.output && data.output.error;
    element('signalStatus').textContent = error || data.message || 'Keine Rückmeldung zur Einstellung';
    element('signalStatus').style.color = !error && data.status === 'success' ? '#39ff14' : 'red';
}

function signalRequestFailed() {
    showSignalResult({status: 'error', message: 'Verbindung fehlgeschlagen; Ausgangszustand unbekannt'});
}

function drawPlot() {
    const canvas = element('pwmOscilloscope');
    const ctx = canvas.getContext('2d');
    if (!ctx || typeof ctx.clearRect !== 'function') return;
    canvas.width = canvas.clientWidth || 640;
    canvas.height = 200;
    const points = plot.data.datasets[0].data;
    const extent = Math.max(1, points[points.length - 1].x);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#39ff14';
    ctx.lineWidth = 2;
    ctx.beginPath();
    let previous = null;
    for (const point of points) {
        const x = point.x / extent * canvas.width;
        const y = point.y ? 20 : 160;
        if (previous) {ctx.lineTo(x, previous.y); ctx.lineTo(x, y);} else ctx.moveTo(x, y);
        previous = {x, y};
    }
    if (points.length === 1) ctx.lineTo(canvas.width, 160);
    ctx.stroke();
    ctx.fillStyle = '#39ff14';
    ctx.font = '12px sans-serif';
    ctx.fillText(`Zeit: 0 bis ${(extent / 1000).toFixed(2)} ms`, 8, 190);
}

function renderOutputState(state) {
    if (!state || !Number.isInteger(state.revision)) return false;
    if (state.instance_id && state.instance_id !== outputControllerId) {
        if (retiredOutputControllers.has(state.instance_id)) return false;
        if (outputControllerId) retiredOutputControllers.add(outputControllerId);
        outputControllerId = state.instance_id;
        outputRevision = -1;
    }
    if (state.revision < outputRevision) return false;
    outputRevision = state.revision;
    const names = {idle: 'Aus', ton_toff: 't_ON/t_OFF', burst: 'Burst', duty_cycle: 'Duty Cycle',
        midi: 'MIDI', single_shot: 'Single Shot', fault: 'Fehler'};
    element('outputMode').textContent = names[state.mode] || 'Unbekannt';
    isPlaying = state.playing;
    element('playStopButton').textContent = isPlaying ? 'Stop' : 'Play';
    let points = [{x: 0, y: 0}];
    if (state.settings) {
        points = [];
        let time = 0;
        for (let i = 0; i < 10; i++) {
            points.push({x: time, y: 1});
            time += state.settings.t_on_us;
            points.push({x: time, y: 0});
            time += state.settings.t_off_ms * 1000;
        }
    }
    plot.data.datasets[0].data = points;
    drawPlot();
    if (state.error) {
        element('signalStatus').textContent = state.error;
        element('signalStatus').style.color = 'red';
    }
    return true;
}

function updateOutputStatus() {
    return api('/output_status').then(state => {
        if (!renderOutputState(state)) throw new Error('Status nicht verfügbar');
    }).catch(() => {element('outputMode').textContent = 'Status nicht erreichbar';});
}

function stopOutput() {
    return api('/stop_output', {method: 'POST'}).then(showSignalResult).catch(signalRequestFailed);
}

function updateTonToff() {
    element('t_on_output').textContent = element('t_on').value;
    element('t_off_output').textContent = element('t_off').value;
    return postForm('/set_ton_toff', {t_on: element('t_on').value, t_off: element('t_off').value})
        .then(showSignalResult).catch(signalRequestFailed);
}

function updateBurstSettings() {
    element('burst_t_on_output').textContent = element('burst_t_on').value;
    element('bps_output').textContent = element('bps').value;
    return postForm('/set_burst', {t_on: element('burst_t_on').value, bps: element('bps').value})
        .then(showSignalResult).catch(signalRequestFailed);
}

async function fireSingleShot() {
    const button = element('single_shot_button');
    if (button.disabled) return;
    button.disabled = true;
    try {
        const result = await postForm('/single_shot', {t_on: element('single_ton').value});
        showSignalResult(result);
        element('single-shot-error-message').textContent = result.status === 'success' ? '' : result.message;
    } catch (_) {signalRequestFailed();} finally {button.disabled = false;}
}

function togglePlayStop() {
    const request = isPlaying ? api('/stop_midi', {method: 'POST'})
        : postForm('/play_midi', {midi_file: element('midi_file').value});
    return request.then(showSignalResult).catch(signalRequestFailed);
}

function updatePowerStatus() {
    return api('/power_status').then(data => {
        if (typeof data.power === 'boolean') element('masterPowerSwitch').checked = data.power;
    }).catch(signalRequestFailed);
}

function checkSoftstartStatus() {
    return api('/softstart_status').then(data => {
        element('softstartStatus').hidden = !data.active;
        element('softstartProgressBar').value = data.progress;
        element('softstartProgressText').textContent = `Softstart aktiv: ${data.progress}%`;
    }).catch(signalRequestFailed);
}

function togglePower() {
    return api('/toggle_power', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({power: element('masterPowerSwitch').checked})})
        .then(data => {showSignalResult(data); updatePowerStatus(); checkSoftstartStatus();})
        .catch(signalRequestFailed);
}

let heartbeatPending = false;
async function heartbeat() {
    if (heartbeatPending) return;
    heartbeatPending = true;
    const start = Date.now();
    try {
        const result = await api('/heartbeat', {method: 'POST'});
        element('connectionStatus').textContent = result.status === 'error' ? result.message
            : result.occupied && !result.controlling ? 'Ein anderes Fenster steuert'
            : `Verbindung: ${Date.now() - start} ms`;
    } catch (_) {element('connectionStatus').textContent = 'Verbindung unterbrochen';}
    finally {heartbeatPending = false;}
}

async function loadMidiFiles() {
    try {
        const data = await api('/get_midi_files');
        const select = element('midi_file');
        for (const file of data.files) {
            const option = document.createElement('option');
            option.value = file;
            option.textContent = file;
            select.appendChild(option);
        }
    } catch (_) {signalRequestFailed();}
}

function debounce(callback, delay = 150) {
    let timer;
    return () => {clearTimeout(timer); timer = setTimeout(callback, delay);};
}

element('stopOutputButton').addEventListener('click', stopOutput);
element('single_shot_button').addEventListener('click', fireSingleShot);
element('playStopButton').addEventListener('click', togglePlayStop);
element('masterPowerSwitch').addEventListener('change', togglePower);
for (const id of ['t_on', 't_off']) element(id).addEventListener('input', debounce(updateTonToff));
for (const id of ['burst_t_on', 'bps']) element(id).addEventListener('input', debounce(updateBurstSettings));
element('single_ton').addEventListener('input', () => {element('single_ton_output').textContent = element('single_ton').value;});
element('midiOnTime').addEventListener('input', debounce(() => {
    element('midiOnTimeValue').textContent = element('midiOnTime').value;
    postForm('/set_midi_max_t_on', {max_t_on: element('midiOnTime').value}).then(showSignalResult).catch(signalRequestFailed);
}));
element('pitchControl').addEventListener('input', debounce(() => {
    element('pitchValue').textContent = element('pitchControl').value;
    postForm('/update_pitch', {pitch: element('pitchControl').value}).then(showSignalResult).catch(signalRequestFailed);
}));
element('logoutButton').addEventListener('click', async () => {
    try {await api('/logout', {method: 'POST'}); window.location.assign('/login');}
    catch (_) {signalRequestFailed();}
});
for (const tab of document.querySelectorAll('[data-tab]')) {
    tab.addEventListener('click', () => {
        for (const panel of document.querySelectorAll('.tab-content')) panel.classList.remove('active');
        for (const button of document.querySelectorAll('[data-tab]')) {
            button.classList.remove('active'); button.setAttribute('aria-selected', 'false');
        }
        element(tab.dataset.tab).classList.add('active');
        tab.classList.add('active'); tab.setAttribute('aria-selected', 'true');
    });
}
window.addEventListener('resize', drawPlot);
loadMidiFiles();
updateOutputStatus();
updatePowerStatus();
checkSoftstartStatus();
heartbeat();
setInterval(heartbeat, 1000);
setInterval(updateOutputStatus, 1000);
setInterval(updatePowerStatus, 1000);
setInterval(checkSoftstartStatus, 1000);
