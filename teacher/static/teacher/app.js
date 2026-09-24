(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;

  const ui = {
    stage: $("stage"), stateLabel: $("state-label"), teacherText: $("teacher-text"),
    reference: $("reference"), focus: $("focus"), targetLabel: $("target-label"),
    sentenceHint: $("sentence-hint"), progress: $("progress"), progressFill: $("progress-fill"), transcriptCard: $("transcript-card"), words: $("words"),
    btnStart: $("btn-start"), btnRecord: $("btn-record"), btnReplay: $("btn-replay"),
    timer: $("timer"), error: $("error"), scores: $("scores"),
    sOverall: $("s-overall"), sAccuracy: $("s-accuracy"), sFluency: $("s-fluency"),
  };

  const avatar = new Avatar(ui.stage);
  avatar.tryLoadRive("/static/teacher/teacher.riv");

  let recorder = null;
  let busy = false;
  let reference = "";

  const setLabel = (t) => { ui.stateLabel.textContent = t; };
  const showError = (msg) => { ui.error.textContent = msg || ""; ui.error.hidden = !msg; };

  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "X-CSRFToken": csrf }, body });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
    return data;
  }

  function renderTarget(data) {
    reference = data.next_reference;
    const word = data.mode === "word";
    ui.targetLabel.textContent = word ? "Só essa palavra:" : "Repete essa:";
    ui.reference.classList.toggle("word-mode", word);
    if (data.progress) {
      const p = data.progress;
      ui.progress.textContent = `${p.done}/${p.total}`;
      ui.progressFill.style.width = `${Math.round((p.done / p.total) * 100)}%`;
      const tries = word ? p.word_tries + 1 : p.sentence_tries + 1;
      ui.sentenceHint.hidden = false;
      ui.sentenceHint.textContent = word
        ? `tentativa ${tries} · depois volta pra: ${data.sentence}`
        : (tries > 1 ? `tentativa ${tries}` : "");
      if (!word && tries <= 1) ui.sentenceHint.hidden = true;
    }
    ui.focus.hidden = !data.focus;
    ui.focus.textContent = data.focus ? `foco: ${data.focus}` : "";
    ui.stage.classList.remove("flash-pass", "flash-word");
    void ui.stage.offsetWidth;
    if (data.event === "sentence_pass" || data.event === "word_pass") ui.stage.classList.add("flash-pass");
    if (data.event === "enter_word") ui.stage.classList.add("flash-word");
  }

  async function playTeacher(data) {
    ui.teacherText.textContent = data.reply;
    avatar.setMood(data.mood);
    renderTarget(data);
    if (data.speech) {
      ui.btnRecord.disabled = true;
      setLabel("falando…");
      await avatar.speak(data.speech.audio_b64, data.speech.visemes);
      ui.btnReplay.hidden = false;
    } else if (data.tts_error) {
      showError("Sem voz: " + data.tts_error);
    }
    renderTarget(data);
    ui.btnRecord.disabled = false;
    setLabel("sua vez — segure o botão");
  }

  function renderWords(summary) {
    ui.words.innerHTML = "";
    if (!summary || !summary.recognized) { ui.transcriptCard.hidden = true; return; }
    for (const w of summary.words) {
      const el = document.createElement("span");
      el.className = "word";
      el.textContent = w.word;
      if (w.error === "Omission") el.classList.add("omitted");
      else if (w.error === "Insertion") el.classList.add("inserted");
      else if (w.score >= 80) el.classList.add("good");
      else if (w.score >= 60) el.classList.add("meh");
      else el.classList.add("bad");
      if (w.bad_phonemes && w.bad_phonemes.length) {
        const s = document.createElement("small");
        s.textContent = w.bad_phonemes.map((p) => `/${p.phoneme}/`).join(" ");
        el.appendChild(s);
      }
      ui.words.appendChild(el);
    }
    ui.transcriptCard.hidden = false;
    const sc = summary.scores;
    ui.sOverall.textContent = sc.overall; ui.sAccuracy.textContent = sc.accuracy; ui.sFluency.textContent = sc.fluency;
    ui.scores.hidden = false;
  }

  // ---- começar ----
  ui.btnStart.addEventListener("click", async () => {
    showError("");
    ui.btnStart.disabled = true;
    setLabel("chamando o professor…");
    try {
      // Pede permissão do mic já no gesto do usuário (iOS exige)
      try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop()); }
      catch (_) { showError("Preciso do microfone para funcionar."); ui.btnStart.disabled = false; return; }
      const data = await post("/api/start", new FormData());
      ui.btnStart.hidden = true;
      ui.btnRecord.hidden = false;
      await playTeacher(data);
    } catch (e) {
      showError(e.message);
      ui.btnStart.disabled = false;
    }
  });

  // ---- segurar para gravar ----
  async function startRecording(ev) {
    ev.preventDefault();
    if (busy || ui.btnRecord.disabled) return;
    busy = true;
    showError("");
    recorder = new WavRecorder({ maxSeconds: 15 });
    recorder.onTick = (s) => { ui.timer.textContent = s.toFixed(1) + "s"; };
    recorder.onMax = () => stopRecording();
    try {
      await recorder.start();
      ui.btnRecord.classList.add("active");
      avatar.setState("listening");
      setLabel("ouvindo…");
    } catch (e) {
      busy = false;
      showError("Não consegui abrir o microfone: " + e.message);
    }
  }

  async function stopRecording() {
    if (!recorder || !recorder.recording) return;
    ui.btnRecord.classList.remove("active");
    const blob = await recorder.stop();
    ui.timer.textContent = "0.0s";
    if (!blob || blob.size < 8000) { // < ~0.25 s
      busy = false; avatar.setState("idle"); setLabel("muito curto, segure mais");
      return;
    }
    avatar.setState("thinking");
    setLabel("Mr. Blake está ouvindo…");
    ui.btnRecord.disabled = true;
    try {
      const fd = new FormData();
      fd.append("audio", blob, "aluno.wav");
      fd.append("reference", reference);
      const data = await post("/api/assess", fd);
      renderWords(data.summary);
      await playTeacher(data);
    } catch (e) {
      showError(e.message);
      avatar.setState("idle");
      ui.btnRecord.disabled = false;
      setLabel("tenta de novo");
    } finally {
      busy = false;
    }
  }

  ui.btnRecord.addEventListener("pointerdown", startRecording);
  ui.btnRecord.addEventListener("pointerup", stopRecording);
  ui.btnRecord.addEventListener("pointercancel", stopRecording);
  ui.btnRecord.addEventListener("pointerleave", () => { if (recorder && recorder.recording) stopRecording(); });
  ui.btnRecord.addEventListener("contextmenu", (e) => e.preventDefault());

  ui.btnReplay.addEventListener("click", () => { if (!busy) avatar.replay(); });
})();
