/**
 * Gravador que produz WAV PCM 16 kHz mono 16-bit direto no navegador,
 * no formato que o Azure Speech quer. Evita ffmpeg no servidor.
 * Usa ScriptProcessorNode (deprecado, mas é o que funciona em todo celular,
 * inclusive Safari iOS). Troque por AudioWorklet quando quiser.
 */
class WavRecorder {
  constructor({ targetRate = 16000, maxSeconds = 15 } = {}) {
    this.targetRate = targetRate;
    this.maxSeconds = maxSeconds;
    this.chunks = [];
    this.recording = false;
    this.onTick = null;
    this.onMax = null;
    this._generation = 0;
    this._starting = false;
  }

  async start() {
    if (this.recording || this._starting) return false;
    this._starting = true;
    const generation = ++this._generation;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      // Reutiliza o contexto, mas abre o microfone somente enquanto o botão é segurado.
      if (!this.ctx || this.ctx.state === "closed") this.ctx = new AC();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (generation !== this._generation) {
        stream.getTracks().forEach((t) => t.stop());
        return false;
      }
      this.stream = stream;
      await this._suspending;
      if (generation !== this._generation) return false;
      if (this.ctx.state === "suspended") await this.ctx.resume();
      if (generation !== this._generation) return false;
      this.sourceRate = this.ctx.sampleRate;
      this.source = this.ctx.createMediaStreamSource(stream);
      this.processor = this.ctx.createScriptProcessor(2048, 1, 1);
      this.chunks = [];
      this.recording = true;
      this.startedAt = performance.now();
      let lastTick = -100;
      this.processor.onaudioprocess = (e) => {
        if (!this.recording) return;
        this.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
        const elapsedMs = performance.now() - this.startedAt;
        if (this.onTick && elapsedMs - lastTick >= 100) {
          lastTick = elapsedMs;
          this.onTick(elapsedMs / 1000);
        }
      };
      this.source.connect(this.processor);
      // Precisa conectar no destination para o processor rodar no Safari.
      this.gain = this.ctx.createGain();
      this.gain.gain.value = 0;
      this.processor.connect(this.gain);
      this.gain.connect(this.ctx.destination);
      this._maxTimer = setTimeout(() => {
        if (this.recording) {
          if (this.onMax) this.onMax();
          else this.stop();
        }
      }, this.maxSeconds * 1000);
      return true;
    } catch (error) {
      this.recording = false;
      throw error;
    } finally {
      this._starting = false;
      if (!this.recording) this._release();
    }
  }

  _release() {
    clearTimeout(this._maxTimer);
    if (this.processor) this.processor.onaudioprocess = null;
    for (const name of ["source", "processor", "gain"]) {
      try { this[name]?.disconnect(); } catch (_) {}
      this[name] = null;
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    // Suspender não bloqueia a codificação nem o envio do WAV.
    if (this.ctx && this.ctx.state !== "closed") {
      this._suspending = this.ctx.suspend().catch(() => {});
    }
  }

  /** Para a gravação e devolve um Blob WAV. */
  async stop() {
    ++this._generation; // Também cancela a abertura assíncrona do microfone.
    const wasRecording = this.recording;
    this.recording = false;
    this._release();
    if (!wasRecording) return null;

    const total = this.chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Float32Array(total);
    let off = 0;
    for (const c of this.chunks) { merged.set(c, off); off += c.length; }
    const down = WavRecorder.downsample(merged, this.sourceRate, this.targetRate);
    this.chunks = [];
    return WavRecorder.encodeWav(down, this.targetRate);
  }

  async close() {
    await this.stop();
    const ctx = this.ctx;
    this.ctx = null;
    if (ctx && ctx.state !== "closed") await ctx.close().catch(() => {});
  }

  static downsample(buffer, fromRate, toRate) {
    if (fromRate === toRate) return buffer;
    const ratio = fromRate / toRate;
    const outLen = Math.round(buffer.length / ratio);
    const out = new Float32Array(outLen);
    let pos = 0;
    for (let i = 0; i < outLen; i++) {
      const next = Math.round((i + 1) * ratio);
      let sum = 0, n = 0;
      for (let j = pos; j < next && j < buffer.length; j++) { sum += buffer[j]; n++; }
      out[i] = n ? sum / n : 0;
      pos = next;
    }
    return out;
  }

  static encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
    str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, samples.length * 2, true);
    let o = 44;
    for (let i = 0; i < samples.length; i++, o += 2) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: "audio/wav" });
  }
}
window.WavRecorder = WavRecorder;
