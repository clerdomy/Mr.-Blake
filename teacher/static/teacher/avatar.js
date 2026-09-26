/**
 * Boneco falante.
 *  - Se existir /static/teacher/teacher.riv, carrega o Rive e manda os inputs
 *    da state machine "Teacher": viseme (number 0-21), mood (number), talking (bool).
 *  - Senão usa o rosto SVG do template, trocando o formato da boca por visema.
 *
 * Visemas do Azure (0-21): https://learn.microsoft.com/azure/ai-services/speech-service/how-to-speech-synthesis-viseme
 */

// Formatos de boca do SVG (viewBox 200x200; boca centrada em x=100, y≈140)
const MOUTHS = {
  closed: { mouth: "M78 140 Q100 146 122 140 Q100 146 78 140 Z", teeth: "", tongue: "" },
  slight: { mouth: "M80 138 Q100 134 120 138 Q100 150 80 138 Z", teeth: "M84 138 Q100 136 116 138 Q100 141 84 138 Z", tongue: "" },
  mid:    { mouth: "M78 136 Q100 130 122 136 Q100 160 78 136 Z", teeth: "M84 136 Q100 134 116 136 Q100 142 84 136 Z", tongue: "M86 152 Q100 144 114 152 Q100 160 86 152 Z" },
  wide:   { mouth: "M74 134 Q100 126 126 134 Q100 170 74 134 Z", teeth: "M82 134 Q100 132 118 134 Q100 142 82 134 Z", tongue: "M84 158 Q100 148 116 158 Q100 168 84 158 Z" },
  round:  { mouth: "M88 134 Q100 128 112 134 Q116 146 100 156 Q84 146 88 134 Z", teeth: "", tongue: "" },
  small:  { mouth: "M92 138 Q100 134 108 138 Q108 148 100 150 Q92 148 92 138 Z", teeth: "", tongue: "" },
  smile:  { mouth: "M74 136 Q100 132 126 136 Q100 152 74 136 Z", teeth: "M80 136 Q100 135 120 136 Q100 143 80 136 Z", tongue: "" },
  teeth:  { mouth: "M80 138 Q100 135 120 138 Q100 147 80 138 Z", teeth: "M82 138 Q100 136 118 138 Q100 145 82 138 Z", tongue: "" },
  bite:   { mouth: "M80 140 Q100 137 120 140 Q100 146 80 140 Z", teeth: "M82 139 Q100 137 118 139 Q100 144 82 139 Z", tongue: "" },
  thtongue:{ mouth: "M80 138 Q100 134 120 138 Q100 150 80 138 Z", teeth: "M84 138 Q100 136 116 138 Q100 141 84 138 Z", tongue: "M92 139 Q100 136 108 139 Q100 146 92 139 Z" },
};

const VISEME_TO_MOUTH = {
  0: "closed", 1: "mid", 2: "wide", 3: "round", 4: "mid", 5: "mid", 6: "smile", 7: "small",
  8: "round", 9: "wide", 10: "round", 11: "wide", 12: "slight", 13: "small", 14: "slight",
  15: "teeth", 16: "small", 17: "thtongue", 18: "bite", 19: "slight", 20: "slight", 21: "closed",
};

const MOOD_TO_NUMBER = { neutral: 0, happy: 1, sassy: 2, shocked: 3 };

class Avatar {
  constructor(stageEl) {
    this.stage = stageEl;
    this.svg = stageEl.querySelector("#face");
    this.canvas = stageEl.querySelector("#rive-canvas");
    this.mouth = stageEl.querySelector("#mouth");
    this.teeth = stageEl.querySelector("#teeth");
    this.tongue = stageEl.querySelector("#tongue");
    this.rive = null;
    this.inputs = {};
    this._raf = null;
    this._startBlinking();
    this._startIdleEyes();
  }

  /** Tenta carregar o Rive; se não houver arquivo, fica no SVG. */
  async tryLoadRive(url) {
    try {
      const head = await fetch(url, { method: "HEAD" });
      if (!head.ok) return false;
      await new Promise((res, rej) => {
        const s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/npm/@rive-app/canvas@2/rive.min.js";
        s.onload = res; s.onerror = rej; document.head.appendChild(s);
      });
      const self = this;
      await new Promise((res, rej) => {
        self.rive = new window.rive.Rive({
          src: url, canvas: self.canvas, autoplay: true, stateMachines: "Teacher",
          onLoad: () => {
            self.rive.resizeDrawingSurfaceToCanvas();
            for (const i of self.rive.stateMachineInputs("Teacher")) self.inputs[i.name] = i;
            res();
          },
          onLoadError: rej,
        });
      });
      this.svg.hidden = true;
      this.canvas.hidden = false;
      return true;
    } catch (e) {
      console.info("Sem Rive, usando rosto SVG.", e?.message || e);
      this.rive = null;
      return false;
    }
  }

  setViseme(id) {
    if (this.rive) { if (this.inputs.viseme) this.inputs.viseme.value = id; return; }
    const shape = MOUTHS[VISEME_TO_MOUTH[id] || "closed"];
    if (this._mouthShape === shape) return;
    this._mouthShape = shape;
    this.mouth.setAttribute("d", shape.mouth);
    this.teeth.setAttribute("d", shape.teeth);
    this.tongue.setAttribute("d", shape.tongue);
  }

  setMood(mood) {
    this.stage.dataset.mood = mood || "neutral";
    if (this.rive && this.inputs.mood) this.inputs.mood.value = MOOD_TO_NUMBER[mood] ?? 0;
  }

  setState(state) { // idle | listening | thinking | talking
    this.stage.dataset.state = state;
    if (this.rive && this.inputs.talking) this.inputs.talking.value = state === "talking";
  }

  /**
   * Toca o áudio (base64 mp3) sincronizando a boca com a lista de visemas [[ms, id], ...].
   * Devolve uma Promise que resolve quando o áudio termina.
   */
  speak(audioB64, visemes) {
    this.stop();
    return new Promise((resolve) => {
      const audio = new Audio("data:audio/mp3;base64," + audioB64);
      this.lastAudio = { audioB64, visemes };
      let idx = 0;
      const self = this;
      const hasVisemes = visemes && visemes.length > 0;

      // Sem visemas (ex.: voz da OpenAI): anima a boca pelo volume do áudio.
      let analyser = null, buf = null, source = null, finished = false;
      if (!hasVisemes) {
        try {
          const AC = window.AudioContext || window.webkitAudioContext;
          self._ctx = self._ctx || new AC();
          if (self._ctx.state === "suspended") self._ctx.resume().catch(() => {});
          source = self._ctx.createMediaElementSource(audio);
          analyser = self._ctx.createAnalyser();
          analyser.fftSize = 512;
          analyser.smoothingTimeConstant = 0.4;
          source.connect(analyser);
          analyser.connect(self._ctx.destination);
          buf = new Uint8Array(analyser.fftSize);
        } catch (e) { console.warn("sem analyser, boca fixa", e); }
      }
      const tick = () => {
        if (finished) return;
        if (hasVisemes) {
          const ms = audio.currentTime * 1000;
          while (idx < visemes.length && visemes[idx][0] <= ms) { self.setViseme(visemes[idx][1]); idx++; }
        } else if (analyser) {
          analyser.getByteTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; sum += d * d; }
          const rms = Math.sqrt(sum / buf.length);
          // rms típico: 0.02 (silêncio) a 0.25 (vogal forte)
          const v = rms < 0.03 ? 0 : rms < 0.07 ? 19 : rms < 0.12 ? 1 : rms < 0.18 ? 2 : 9;
          self.setViseme(v);
        } else {
          // Fallback: abre e fecha no ritmo
          self.setViseme(Math.floor(audio.currentTime * 8) % 2 ? 1 : 0);
        }
        if (!audio.ended && !audio.paused) self._raf = requestAnimationFrame(tick);
      };
      audio.onplaying = () => {
        cancelAnimationFrame(self._raf);
        self.setState("talking"); tick();
      };
      const done = () => {
        if (finished) return;
        finished = true;
        cancelAnimationFrame(self._raf);
        audio.onplaying = audio.onended = audio.onerror = null;
        audio.pause();
        source?.disconnect();
        analyser?.disconnect();
        self._stopPlayback = null;
        self.setViseme(0); self.setState("idle");
        resolve();
      };
      self._stopPlayback = done;
      audio.onended = done;
      audio.onerror = done;
      audio.play().catch((e) => { console.warn("autoplay bloqueado?", e); done(); });
    });
  }

  replay() {
    if (this.lastAudio) return this.speak(this.lastAudio.audioB64, this.lastAudio.visemes);
    return Promise.resolve();
  }

  stop() {
    if (this._stopPlayback) this._stopPlayback();
  }

  // ---- vida no rosto SVG ----
  _startBlinking() {
    const lids = [this.stage.querySelector("#lid-l"), this.stage.querySelector("#lid-r")];
    if (!lids[0]) return;
    const blink = () => {
      if (!document.hidden && !this.rive) {
        lids.forEach((l) => l.setAttribute("ry", 11));
        setTimeout(() => lids.forEach((l) => l.setAttribute("ry", 0)), 120);
      }
      setTimeout(blink, 2500 + Math.random() * 3000);
    };
    setTimeout(blink, 1500);
  }

  _startIdleEyes() {
    const p = [this.stage.querySelector("#pupil-l"), this.stage.querySelector("#pupil-r")];
    if (!p[0]) return;
    const look = () => {
      if (!document.hidden && !this.rive) {
        const dx = (Math.random() - 0.5) * 5, dy = (Math.random() - 0.5) * 3;
        p[0].setAttribute("cx", 80 + dx); p[0].setAttribute("cy", 98 + dy);
        p[1].setAttribute("cx", 124 + dx); p[1].setAttribute("cy", 98 + dy);
      }
      setTimeout(look, 1200 + Math.random() * 2500);
    };
    look();
  }
}
window.Avatar = Avatar;
