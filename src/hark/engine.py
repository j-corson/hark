"""Local speech pipeline: voice activity -> utterances -> text + voice embedding."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx as so

from . import models

SR = 16000
WINDOW = 512  # silero VAD window
PRE_ROLL = int(0.35 * SR)  # VAD fires a little late; keep the first syllable
POST_ROLL = int(0.15 * SR)
MIN_EMBED_SECONDS = 0.9  # shorter clips give unreliable voice embeddings


class Engine:
    def __init__(self, asr: str = "parakeet", threads: int | None = None):
        self.asr_name = asr
        p = models.ensure(asr)
        n = threads or max(1, min(4, (os.cpu_count() or 2)))
        if asr == "parakeet":
            self.recognizer = so.OfflineRecognizer.from_transducer(
                encoder=p["asr_encoder"], decoder=p["asr_decoder"], joiner=p["asr_joiner"],
                tokens=p["asr_tokens"], num_threads=n, model_type="nemo_transducer")
        else:
            self.recognizer = so.OfflineRecognizer.from_moonshine_v2(
                encoder=p["asr_encoder"], decoder=p["asr_decoder"], tokens=p["asr_tokens"], num_threads=n)
        self.extractor = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=p["speaker"], num_threads=n))
        self.vad_config = so.VadModelConfig(
            silero_vad=so.SileroVadModelConfig(model=p["vad"], threshold=0.5, min_silence_duration=0.6,
                                               min_speech_duration=0.25, max_speech_duration=20),
            sample_rate=SR)
        # One worker: keeps utterances in order and the CPU load predictable.
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="hark-asr")
        self.backlog = 0.0  # seconds of speech waiting to be transcribed

    def new_vad(self) -> so.VoiceActivityDetector:
        return so.VoiceActivityDetector(self.vad_config, buffer_size_in_seconds=120)

    def _process(self, samples: np.ndarray) -> tuple[str, np.ndarray | None]:
        st = self.recognizer.create_stream()
        st.accept_waveform(SR, samples)
        self.recognizer.decode_stream(st)
        text = st.result.text.strip()
        emb = None
        if text and len(samples) >= MIN_EMBED_SECONDS * SR:
            es = self.extractor.create_stream()
            es.accept_waveform(SR, samples)
            es.input_finished()
            emb = np.array(self.extractor.compute(es), dtype=np.float32)
        return text, emb

    async def process(self, samples: np.ndarray) -> tuple[str, np.ndarray | None]:
        secs = len(samples) / SR
        self.backlog += secs
        try:
            return await asyncio.get_running_loop().run_in_executor(self.pool, self._process, samples)
        finally:
            self.backlog -= secs


class AudioStream:
    """One audio source (a mic, or call audio) cut into utterances by the VAD."""

    def __init__(self, engine: Engine, source: str):
        self.engine = engine
        self.source = source
        self.vad = engine.new_vad()
        self.t0: float | None = None  # wall-clock time of sample 0
        self.buf = np.zeros(0, dtype=np.float32)
        self.buf_start = 0  # absolute sample index of buf[0]
        self.fed = 0  # samples given to the VAD so far
        self.pending = np.zeros(0, dtype=np.float32)
        self.last_speaker: str | None = None

    def feed(self, pcm: np.ndarray) -> list[tuple[float, np.ndarray]]:
        """Add audio; return finished utterances as (start_time, samples)."""
        if self.t0 is None:
            self.t0 = time.time()
        self.buf = np.concatenate([self.buf, pcm])
        self.pending = np.concatenate([self.pending, pcm])
        n = len(self.pending) // WINDOW * WINDOW
        for i in range(0, n, WINDOW):
            self.vad.accept_waveform(self.pending[i:i + WINDOW])
        self.fed += n
        self.pending = self.pending[n:]
        out = self._drain()
        # Keep ~40 s of history for pre-roll; drop the rest.
        keep = 40 * SR
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            self.buf = self.buf[cut:]
            self.buf_start += cut
        return out

    def flush(self) -> list[tuple[float, np.ndarray]]:
        self.vad.flush()
        return self._drain()

    def _drain(self) -> list[tuple[float, np.ndarray]]:
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            start, n = seg.start, len(seg.samples)
            a = max(start - PRE_ROLL, self.buf_start)
            b = min(start + n + POST_ROLL, self.buf_start + len(self.buf))
            if b - a < n:  # history already trimmed; fall back to the VAD's copy
                samples = np.array(seg.samples, dtype=np.float32)
            else:
                samples = self.buf[a - self.buf_start:b - self.buf_start].copy()
            out.append((self.t0 + start / SR, samples))
            self.vad.pop()
        return out
