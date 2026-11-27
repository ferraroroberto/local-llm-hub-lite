"""A stand-in for ``foundry_local_sdk`` 1.x: the catalog, variants and a live ASR session.

Installed into ``sys.modules`` by :func:`install`, so ``src.foundry.runtime``
imports it exactly as it would the real SDK; no native runtime, model cache
or network is touched.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ASR_VARIANT = "nemotron-3.5-asr-streaming-0.6b-generic-cpu:3"
OFFLINE_VARIANT = "parakeet-tdt-0.6b-v2-generic-cpu:1"


@dataclass
class Part:
    text: str


@dataclass
class Result:
    content: List[Part]
    is_final: bool


class FakeSession:
    def __init__(self, variant: "FakeVariant") -> None:
        self.variant = variant
        self.settings = types.SimpleNamespace(sample_rate=16000, channels=1, language=None)
        self.pushed = bytearray()
        self.stopped = threading.Event()

    def start(self) -> None:
        self.variant.sessions.append(self)

    def append(self, pcm: bytes) -> None:
        self.pushed.extend(pcm)

    def stop(self) -> None:
        self.stopped.set()

    def get_stream(self):
        self.stopped.wait(timeout=5)
        yield Result([Part(" partial")], False)
        yield Result([Part(f" heard {len(self.pushed)} bytes")], True)


class FakeAudioClient:
    def __init__(self, variant: "FakeVariant") -> None:
        self.variant = variant
        self.settings = types.SimpleNamespace(language=None)

    def transcribe(self, audio_file_path: str):
        """File transcription, recording ``(path, bytes, language)``."""
        with open(audio_file_path, "rb") as fh:
            data = fh.read()
        self.variant.files.append((audio_file_path, data, self.settings.language))
        return types.SimpleNamespace(text=f" read {len(data)} bytes ")

    def create_live_transcription_session(self) -> FakeSession:
        return FakeSession(self.variant)


@dataclass
class FakeVariant:
    id: str
    is_cached: bool = True
    is_loaded: bool = False
    loads: int = 0
    downloads: int = 0
    sessions: List[FakeSession] = field(default_factory=list)
    files: List[tuple] = field(default_factory=list)

    def load(self) -> None:
        self.loads += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def download(self, progress=None) -> None:
        self.downloads += 1
        self.is_cached = True

    def get_audio_client(self) -> FakeAudioClient:
        return FakeAudioClient(self)


class FakeCatalog:
    def __init__(self, variants: Dict[str, FakeVariant], redirect: Optional[Dict[str, str]] = None) -> None:
        self.variants = variants
        # id asked -> id returned, to model a catalog that hands back another variant.
        self.redirect = redirect or {}

    def get_model_variant(self, model_id: str) -> Optional[FakeVariant]:
        return self.variants.get(self.redirect.get(model_id, model_id))

    def get_model(self, alias: str) -> Optional[FakeVariant]:
        """The variant an alias (the id without ``:version``) selects, as the SDK's ``get_model`` does."""
        return next((v for key, v in self.variants.items() if key.split(":")[0] == alias), None)


def install(monkeypatch, catalog: FakeCatalog) -> types.ModuleType:
    """Put a fake ``foundry_local_sdk`` serving ``catalog`` into ``sys.modules``."""
    module = types.ModuleType("foundry_local_sdk")

    class Configuration:
        def __init__(self, app_name: str, model_cache_dir: Optional[str] = None, **_: object) -> None:
            self.app_name = app_name
            self.model_cache_dir = model_cache_dir

    class FoundryLocalManager:
        instance = None
        initialized_with: List[str] = []
        cache_dirs: List[Optional[str]] = []

        @staticmethod
        def initialize(config: Configuration) -> None:
            FoundryLocalManager.initialized_with.append(config.app_name)
            FoundryLocalManager.cache_dirs.append(config.model_cache_dir)
            FoundryLocalManager.instance = types.SimpleNamespace(catalog=catalog)

    module.Configuration = Configuration
    module.FoundryLocalManager = FoundryLocalManager
    monkeypatch.setitem(sys.modules, "foundry_local_sdk", module)
    return module


def uninstall(monkeypatch) -> None:
    """Make ``import foundry_local_sdk`` fail, as on a PC without the extra."""
    monkeypatch.setitem(sys.modules, "foundry_local_sdk", None)
