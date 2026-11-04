import argparse
from types import SimpleNamespace

import pytest

from hark import cli, engine


def test_default_thread_count_leaves_cores_free(monkeypatch):
    for cores, want in ((1, 1), (2, 1), (4, 2), (8, 4), (16, 4), (None, 1)):
        monkeypatch.setattr(engine.os, "cpu_count", lambda c=cores: c)
        assert engine.default_threads() == want


def fake_sherpa(seen: list[int]):
    """Stand-in for sherpa-onnx: records the thread count each model is built with."""
    record = lambda **kw: seen.append(kw["num_threads"])
    return SimpleNamespace(
        OfflineRecognizer=SimpleNamespace(from_transducer=record, from_moonshine_v2=record),
        SpeakerEmbeddingExtractor=lambda cfg: None,
        SpeakerEmbeddingExtractorConfig=record,
        VadModelConfig=lambda **kw: None,
        SileroVadModelConfig=lambda **kw: None)


def build(monkeypatch, threads=None, asr="parakeet"):
    seen: list[int] = []
    monkeypatch.setattr(engine, "so", fake_sherpa(seen))
    monkeypatch.setattr(engine.models, "ensure", lambda a: dict.fromkeys(
        ("asr_encoder", "asr_decoder", "asr_joiner", "asr_tokens", "speaker", "vad"), "model.onnx"))
    engine.Engine(asr, threads)
    return seen


def test_thread_count_goes_to_both_speech_models(monkeypatch):
    assert build(monkeypatch, 3) == [3, 3]
    assert build(monkeypatch, 3, asr="moonshine") == [3, 3]
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 4)
    assert build(monkeypatch) == [2, 2]


def test_threads_flag_beats_env_var(monkeypatch):
    monkeypatch.delenv("HARK_THREADS", raising=False)
    assert cli.parser().parse_args([]).threads is None  # unset: Engine picks the default
    assert cli.parser().parse_args(["--threads", "3"]).threads == 3
    monkeypatch.setenv("HARK_THREADS", "2")
    assert cli.parser().parse_args([]).threads == 2
    assert cli.parser().parse_args(["--threads", "3"]).threads == 3


def test_a_thread_count_must_be_a_whole_number_of_at_least_one(monkeypatch):
    monkeypatch.delenv("HARK_THREADS", raising=False)
    for bad in ("0", "-1", "1.5", "lots", ""):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(["--threads", bad])
    for bad in ("0", "two"):
        monkeypatch.setenv("HARK_THREADS", bad)
        with pytest.raises(SystemExit):
            cli.parser().parse_args([])
    with pytest.raises(argparse.ArgumentTypeError, match="whole number"):
        cli.thread_count("0")
