"""Cache LRU curto de áudio do professor, limitado por bytes e entradas."""
from collections import OrderedDict
from threading import Lock
from time import monotonic


class AudioCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.size = 0
        self.lock = Lock()

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            expires, audio, visemes, weight = entry
            if expires <= monotonic():
                del self.entries[key]
                self.size -= weight
                return None
            self.entries.move_to_end(key)
            return {"audio_mp3": audio, "visemes": [list(v) for v in visemes]}

    def put(self, key, result, *, ttl, max_bytes):
        audio = result["audio_mp3"]
        visemes = tuple(tuple(v) for v in result["visemes"])
        weight = len(audio) + len(visemes) * 128 + 256
        if ttl <= 0 or weight > max_bytes:
            return
        with self.lock:
            previous = self.entries.pop(key, None)
            if previous:
                self.size -= previous[3]
            self.entries[key] = (monotonic() + ttl, audio, visemes, weight)
            self.size += weight
            while self.size > max_bytes or len(self.entries) > 64:
                _, old = self.entries.popitem(last=False)
                self.size -= old[3]


audio_cache = AudioCache()
