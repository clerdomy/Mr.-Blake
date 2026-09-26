// Sem dependências: node --test scripts/test_frontend.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = name => fs.readFileSync(path.join(__dirname, '../teacher/static/teacher', name), 'utf8');
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
};

function recorderEnv(getUserMedia) {
  const stats = { contexts: 0, stopped: 0, closed: 0, bufferSize: 0 };
  const node = () => ({ connect() {}, disconnect() {}, gain: { value: 0 } });
  class AudioContext {
    constructor() { stats.contexts++; this.state = 'running'; this.sampleRate = 48000; }
    createMediaStreamSource() { return node(); }
    createScriptProcessor(size) { stats.bufferSize = size; return node(); }
    createGain() { return node(); }
    suspend() { this.state = 'suspended'; return Promise.resolve(); }
    resume() { this.state = 'running'; return Promise.resolve(); }
    close() { this.state = 'closed'; stats.closed++; return Promise.resolve(); }
  }
  const stream = { getTracks: () => [{ stop() { stats.stopped++; } }] };
  const context = {
    window: { AudioContext }, navigator: { mediaDevices: { getUserMedia: getUserMedia || (async () => stream) } },
    performance: { now: () => 0 }, Blob, setTimeout: () => 1, clearTimeout() {},
  };
  vm.runInNewContext(code('recorder.js'), context);
  return { recorder: new context.window.WavRecorder(), stream, stats };
}

test('gravações reutilizam contexto, liberam o microfone e produzem WAV 16k', async () => {
  const { recorder, stats } = recorderEnv();
  for (let i = 0; i < 3; i++) {
    assert.equal(await recorder.start(), true);
    recorder.chunks = [new Float32Array(48000)];
    const wav = await recorder.stop();
    const data = new DataView(await wav.arrayBuffer());
    assert.equal(data.getUint32(24, true), 16000);
    assert.equal(data.getUint32(40, true), 32000);
  }
  assert.equal(stats.contexts, 1);
  assert.equal(stats.closed, 0);
  assert.equal(stats.stopped, 3);
  await recorder.close();
  assert.equal(stats.closed, 1);
});

test('soltar antes de getUserMedia terminar cancela gravação e fecha a captura', async () => {
  const mic = deferred();
  const { recorder, stream, stats } = recorderEnv(() => mic.promise);
  const starting = recorder.start();
  assert.equal(await recorder.stop(), null);
  mic.resolve(stream);
  assert.equal(await starting, false);
  assert.equal(recorder.recording, false);
  assert.equal(stats.stopped, 1);
});

test('WAV é devolvido sem esperar suspensão do dispositivo', async () => {
  const { recorder } = recorderEnv();
  await recorder.start();
  const suspending = deferred();
  recorder.ctx.suspend = () => suspending.promise;
  recorder.chunks = [new Float32Array(4800)];
  const result = await Promise.race([recorder.stop(), new Promise(r => setImmediate(() => r(null)))]);
  assert.ok(result instanceof Blob);
  suspending.resolve();
});

function avatarEnv() {
  const stats = { connections: 0, writes: 0, frames: 0, audios: [] };
  const node = () => ({
    connect() { stats.connections++; }, disconnect() { stats.connections--; },
    getByteTimeDomainData(buf) { buf.fill(128); },
  });
  class AudioContext {
    constructor() { this.state = 'running'; }
    createMediaElementSource() { return node(); }
    createAnalyser() { return node(); }
  }
  class Audio {
    constructor() { stats.audios.push(this); this.currentTime = 0; this.paused = true; }
    play() { this.paused = false; this.onplaying?.(); return Promise.resolve(); }
    pause() { this.paused = true; }
  }
  const shape = { setAttribute() { stats.writes++; } };
  const stage = { dataset: {}, querySelector: name => name.includes('lid-') || name.includes('pupil-') ? null : shape };
  const context = {
    window: { AudioContext }, document: { hidden: false }, Audio, console, Uint8Array,
    requestAnimationFrame() { stats.frames++; return 1; }, cancelAnimationFrame() {},
  };
  vm.runInNewContext(code('avatar.js'), context);
  return { avatar: new context.window.Avatar(stage), stats };
}

test('reprodução desconecta nós de áudio e não acumula recursos entre rodadas', async () => {
  const { avatar, stats } = avatarEnv();
  for (let i = 0; i < 10; i++) {
    const playing = avatar.speak('YQ==', []);
    assert.equal(stats.connections, 2);
    stats.audios.at(-1).onended();
    await playing;
    assert.equal(stats.connections, 0);
  }
});

test('nova reprodução para a anterior e visemas iguais não reescrevem SVG', async () => {
  const { avatar, stats } = avatarEnv();
  const first = avatar.speak('YQ==', [[0, 1]]);
  const second = avatar.speak('Yg==', [[0, 1]]);
  await first;
  assert.equal(stats.audios[0].paused, true);
  avatar.setViseme(1);
  const writes = stats.writes;
  avatar.setViseme(4); // Mesma forma de boca.
  assert.equal(stats.writes, writes);
  avatar.stop();
  await second;
});

function workerEnv() {
  const events = {};
  let fetches = 0;
  const cached = { cached: true };
  const context = {
    URL, self: { location: { origin: 'https://example.com' }, addEventListener: (name, fn) => { events[name] = fn; } },
    caches: { open: async () => ({ match: async () => cached }) },
    fetch: async () => { fetches++; return { ok: false, status: 404 }; },
  };
  vm.runInNewContext(code('sw.js'), context);
  return { events, cached, fetches: () => fetches };
}

test('assets com hash são servidos do cache sem requisição de rede', async () => {
  const env = workerEnv();
  let response;
  env.events.fetch({ request: { method: 'GET', url: 'https://example.com/static/teacher/app.012345abcdef.js' },
    respondWith: promise => { response = promise; } });
  assert.equal(await response, env.cached);
  assert.equal(env.fetches(), 0);
});

test('service worker não intercepta API, HTML ou recursos de outros domínios', () => {
  const env = workerEnv();
  for (const url of ['https://example.com/', 'https://example.com/api/start', 'https://cdn.example.com/static/a.js']) {
    env.events.fetch({ request: { method: 'GET', url }, respondWith() { assert.fail('requisição indevida no cache'); } });
  }
});

test('fluxo da tela cancela toque curto e reutiliza gravador na próxima tentativa', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', classList: { add() {}, remove() {}, toggle() {} }, style: {},
      addEventListener(name, fn) { this[name] = fn; },
    });
    return elements.get(id);
  };
  const opening = deferred();
  let recorders = 0, starts = 0, stops = 0, requests = 0;
  class Recorder {
    constructor() { recorders++; }
    async start() {
      starts++;
      if (starts === 1) return opening.promise;
      this.recording = true;
      return true;
    }
    async stop() { stops++; this.recording = false; return null; }
  }
  const context = {
    document: { getElementById: element, querySelector: () => ({ content: 'csrf' }), addEventListener() {} },
    window: { addEventListener() {} },
    Avatar: class { tryLoadRive() {} setState() {} }, WavRecorder: Recorder,
    fetch() { requests++; },
  };
  vm.runInNewContext(code('app.js'), context);
  const button = element('btn-record');
  const starting = button.pointerdown({ preventDefault() {} });
  await button.pointerup();
  opening.resolve(false);
  await starting;
  await button.pointerdown({ preventDefault() {} });
  await button.pointerup();
  assert.equal(recorders, 1);
  assert.equal(starts, 2);
  assert.ok(stops >= 2);
  assert.equal(requests, 0);
  assert.match(element('state-label').textContent, /muito curto/);
});
