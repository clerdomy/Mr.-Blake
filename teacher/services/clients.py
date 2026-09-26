"""Reutiliza conexões HTTP, isolando clientes por processo e configuração."""
import os
from functools import lru_cache

from openai import OpenAI


@lru_cache(maxsize=4)
def _client(pid, base_url, api_key, timeout, max_retries):
    options = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
    if base_url:
        options["base_url"] = base_url
    return OpenAI(**options)


def get_client(api_key, *, base_url=None, timeout=30.0, max_retries=0):
    return _client(os.getpid(), base_url, api_key, timeout, max_retries)
