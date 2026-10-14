const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

(async () => {
    const html = fs.readFileSync(process.argv[2], 'utf8');
    const elements = {};
    for (const tag of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
        const attributes = {};
        for (const attr of tag[0].matchAll(/\b([\w-]+)="([^"]*)"/g)) {
            attributes[attr[1]] = attr[2];
        }
        elements[tag[1]] = {
            ...attributes, style: {}, listeners: {}, classList: {add() {}, remove() {}},
            addEventListener(kind, callback) {(this.listeners[kind] ||= []).push(callback);},
            getContext() {return {};}, click() {},
        };
    }
    const requests = [];
    const charts = [];
    let reply = {status: 'success', message: 'OK'};
    let httpOK = true;
    let httpStatus = 200;
    const redirects = [];
    const csrfToken = html.match(/name="csrf-token" content="([^"]*)"/)[1];
    const context = vm.createContext({
        console: {log() {}, error() {}},
        document: {
            getElementById(id) {return elements[id] || null;},
            getElementsByClassName() {return [];},
            querySelector() {return {content: csrfToken};},
            querySelectorAll() {return [];},
        },
        window: {innerWidth: 1000, addEventListener() {}, location: {assign(url) {redirects.push(url);}},
            crypto: {randomUUID() {return '00000000-0000-4000-8000-000000000001';}}},
        setInterval() {}, setTimeout(callback, delay) {if (delay === 150) callback();},
        clearTimeout() {}, URLSearchParams, AbortController,
        Chart: class {
            constructor(ctx, config) {Object.assign(this, config); charts.push(this);}
            resize() {} update() {}
        },
        fetch(url, options) {
            requests.push({url, options});
            const data = reply;
            return Promise.resolve({ok: httpOK, status: httpStatus, json: () => Promise.resolve(data)});
        },
    });
    vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), context);
    const state = (instance_id, revision, mode, extra = {}) => ({
        instance_id, revision, mode, playing: mode === 'midi',
        burst_active: mode === 'burst', settings: null, error: null, ...extra,
    });
    const render = data => {
        context.testState = data;
        return vm.runInContext('renderOutputState(testState)', context);
    };
    const flush = () => new Promise(setImmediate);
    await flush();

    assert.equal(elements.single_ton.max, '200');
    assert.equal(elements.t_off.min, '5');
    assert(!Object.keys(elements).some(id => id.startsWith('cw')));

    render(state('server-a', 10, 'burst', {settings: {t_on_us: 200, t_off_ms: 19.8}}));
    assert.equal(elements.outputMode.textContent, 'Burst');
    assert.equal(elements.playStopButton.textContent, 'Play');
    assert.equal(vm.runInContext('plot.data.datasets[0].data[1].x', context), 200);
    assert.equal(render(state('server-a', 9, 'midi')), false);
    assert.equal(elements.outputMode.textContent, 'Burst');

    render(state('server-a', 11, 'midi'));
    assert.equal(elements.outputMode.textContent, 'MIDI');
    assert.equal(elements.playStopButton.textContent, 'Stop');
    elements.signalStatus.textContent = 'Current response';
    context.testReply = {status: 'success', message: 'Old response', output: state('server-a', 10, 'burst')};
    vm.runInContext('showSignalResult(testReply)', context);
    assert.equal(elements.signalStatus.textContent, 'Current response');

    render(state('server-b', 0, 'idle'));
    assert.equal(elements.outputMode.textContent, 'Aus');
    assert.equal(elements.playStopButton.textContent, 'Play');
    assert.equal(render(state('server-a', 99, 'midi')), false);
    assert.equal(elements.outputMode.textContent, 'Aus');

    context.testReply = {
        status: 'success', message: 'No MIDI playing',
        output: state('server-b', 1, 'fault', {error: 'Hardware fault'}),
    };
    vm.runInContext('showSignalResult(testReply)', context);
    assert.equal(elements.outputMode.textContent, 'Fehler');
    assert.equal(elements.signalStatus.textContent, 'Hardware fault');
    assert.equal(elements.signalStatus.style.color, 'red');

    reply = {status: 'success', message: 'Stopped', output: state('server-b', 2, 'idle')};
    for (const callback of elements.stopOutputButton.listeners.click || []) callback();
    await flush();
    assert.equal(requests.filter(r => r.url === '/stop_output').length, 1);
    assert.equal(elements.outputMode.textContent, 'Aus');
    assert.equal(elements.signalStatus.textContent, 'Stopped');

    for (const callback of elements.single_shot_button.listeners.click || []) callback();
    for (const callback of elements.single_shot_button.listeners.click || []) callback();
    await flush();
    assert.equal(requests.filter(r => r.url === '/single_shot').length, 1);
    assert.equal(requests.find(r => r.url === '/single_shot').options.headers['X-CSRF-Token'], csrfToken);
    assert.equal(requests.find(r => r.url === '/single_shot').options.headers['X-Control-Tab'],
        '00000000-0000-4000-8000-000000000001');
    assert.equal(elements.single_shot_button.disabled, false);

    reply = {status: 'error', message: 'Duty exceeded', output: state('server-b', 2, 'idle')};
    vm.runInContext('updateTonToff()', context);
    await flush();
    assert.equal(elements.signalStatus.textContent, 'Duty exceeded');
    assert.equal(elements.signalStatus.style.color, 'red');

    httpOK = false;
    vm.runInContext('updateOutputStatus()', context);
    await flush();
    assert.equal(elements.outputMode.textContent, 'Status nicht erreichbar');
    httpOK = true;
    reply = state('server-b', 3, 'burst');
    vm.runInContext('updateOutputStatus()', context);
    await flush();
    assert.equal(elements.outputMode.textContent, 'Burst');
    assert(!html.includes('onclick='));
    assert(!html.includes('cdn.jsdelivr'));
    httpStatus = 401;
    httpOK = false;
    vm.runInContext('updateOutputStatus()', context);
    await flush();
    assert.equal(redirects[0], '/login');
    console.log('UI simulation passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
