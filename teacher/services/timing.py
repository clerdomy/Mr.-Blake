"""Mede etapas da requisição sem registrar fala do aluno ou credenciais."""
import logging
from contextlib import contextmanager
from functools import wraps
from time import perf_counter

log = logging.getLogger(__name__)


class RequestTiming:
    def __init__(self):
        self.started = perf_counter()
        self.durations = {}

    @contextmanager
    def measure(self, name):
        started = perf_counter()
        try:
            yield
        finally:
            self.durations[name] = (perf_counter() - started) * 1000

    def header(self):
        durations = {**self.durations, "total": (perf_counter() - self.started) * 1000}
        return ", ".join(f"{name};dur={duration:.1f}" for name, duration in durations.items())


def timed_request(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request.timings = RequestTiming()
        response = view(request, *args, **kwargs)
        response["Server-Timing"] = request.timings.header()
        log.info("request=%s status=%s timing=%s", request.path,
                 response.status_code, response["Server-Timing"])
        return response
    return wrapped
