"""
Normalização de áudio: garante WAV PCM 16 kHz mono 16-bit, que é o que o
Azure Speech espera. O front já grava nesse formato; isto é o fallback
(ex.: navegador que só entrega webm/opus ou m4a).
"""
import shutil
import subprocess
import tempfile
from pathlib import Path


def _is_wav_16k_mono(data: bytes) -> bool:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False
    # Cabeçalho canônico: fmt chunk começa no byte 12
    audio_format = int.from_bytes(data[20:22], "little")
    channels = int.from_bytes(data[22:24], "little")
    sample_rate = int.from_bytes(data[24:28], "little")
    bits = int.from_bytes(data[34:36], "little")
    return audio_format == 1 and channels == 1 and sample_rate == 16000 and bits == 16


def to_wav_16k(data: bytes) -> bytes:
    """Devolve bytes WAV 16k mono 16-bit, convertendo com ffmpeg se necessário."""
    if _is_wav_16k_mono(data):
        return data
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "Áudio não está em WAV 16 kHz mono e o ffmpeg não está instalado para converter."
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.bin"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(data)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-f", "wav",
                str(dst),
            ],
            check=True,
        )
        return dst.read_bytes()
