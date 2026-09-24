"""
Teste de ponta a ponta sem navegador.

    python scripts/test_assess.py caminho/do/audio.wav "I think the weather is really nice today."

Grave um WAV com qualquer coisa (o backend converte com ffmpeg se precisar),
rode e veja: o JSON bruto do Azure, o resumo, e a resposta do professor.
Precisa do .env carregado (export $(cat .env | xargs)) ou das variáveis no ambiente.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from teacher.services import azure_speech, llm  # noqa: E402
from teacher.services.audio import to_wav_16k  # noqa: E402
from teacher.services.summarize import summarize  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    audio_path, reference = sys.argv[1], sys.argv[2]

    wav = to_wav_16k(Path(audio_path).read_bytes())
    print(f"[1] áudio ok: {len(wav)} bytes")

    assessment = azure_speech.assess_pronunciation(wav, reference)
    print(f"[2] Azure reconheceu: {assessment['text']!r}")
    Path("last_raw.json").write_text(json.dumps(assessment["raw"], indent=2, ensure_ascii=False))
    print("    JSON bruto salvo em last_raw.json")

    summary = summarize(assessment)
    print("[3] resumo:")
    print(json.dumps({k: v for k, v in summary.items() if k != "words"}, indent=2, ensure_ascii=False))

    reply = llm.teacher_reply(reference, summary, history=[])
    print("[4] professor:")
    print(json.dumps({k: v for k, v in reply.items() if not k.startswith("_")}, indent=2, ensure_ascii=False))

    if "--tts" in sys.argv:
        tts = azure_speech.synthesize_with_visemes(reply["reply"])
        Path("last_reply.mp3").write_bytes(tts["audio_mp3"])
        print(f"[5] TTS: last_reply.mp3 ({len(tts['audio_mp3'])} bytes), {len(tts['visemes'])} visemas")


if __name__ == "__main__":
    main()
