"""Speech models: where they come from and where they live on disk.

Everything runs locally with sherpa-onnx (no PyTorch). Models download once,
on first run, into ~/.cache/hark/models (override with HARK_MODELS).
"""

from __future__ import annotations

import os
import sys
import tarfile
import urllib.request
from pathlib import Path

REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

ASR = {
    # Accurate, punctuated, 25 European languages. ~490 MB. Default.
    "parakeet": {
        "archive": f"{REL}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
        "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "files": {"encoder": "encoder.int8.onnx", "decoder": "decoder.int8.onnx", "joiner": "joiner.int8.onnx", "tokens": "tokens.txt"},
        "size": "490 MB",
    },
    # Smaller and faster, English only. ~110 MB. For slow machines.
    "moonshine": {
        "archive": f"{REL}/asr-models/sherpa-onnx-moonshine-base-en-quantized-2026-02-27.tar.bz2",
        "dir": "sherpa-onnx-moonshine-base-en-quantized-2026-02-27",
        "files": {"encoder": "encoder_model.ort", "decoder": "decoder_model_merged.ort", "tokens": "tokens.txt"},
        "size": "110 MB",
    },
}

SINGLE = {
    "vad": (f"{REL}/asr-models/silero_vad.onnx", "silero_vad.onnx"),
    # Voice embeddings for telling speakers apart. ~26 MB.
    "speaker": (f"{REL}/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx", "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"),
}


def model_dir() -> Path:
    d = Path(os.environ.get("HARK_MODELS", Path.home() / ".cache" / "hark" / "models"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch(url: str, dest: Path, label: str) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {label} ...", file=sys.stderr, flush=True)

    def hook(blocks, bs, total):
        if total > 0:
            pct = min(100, blocks * bs * 100 // total)
            print(f"\r  {label}: {pct:3d}%", end="", file=sys.stderr, flush=True)

    urllib.request.urlretrieve(url, tmp, hook)
    print(file=sys.stderr)
    tmp.replace(dest)


def ensure(asr: str) -> dict[str, str]:
    """Download anything missing and return paths to every model file."""
    if asr not in ASR:
        raise SystemExit(f"Unknown speech model '{asr}'. Choose from: {', '.join(ASR)}")
    root = model_dir()
    paths: dict[str, str] = {}
    for key, (url, name) in SINGLE.items():
        p = root / name
        if not p.exists():
            _fetch(url, p, name)
        paths[key] = str(p)
    spec = ASR[asr]
    d = root / spec["dir"]
    if not all((d / f).exists() for f in spec["files"].values()):
        arc = root / (spec["dir"] + ".tar.bz2")
        _fetch(spec["archive"], arc, f"{asr} speech model ({spec['size']})")
        print("  unpacking ...", file=sys.stderr, flush=True)
        with tarfile.open(arc) as t:
            t.extractall(root, filter="data")
        arc.unlink()
    for k, f in spec["files"].items():
        paths[f"asr_{k}"] = str(d / f)
    return paths
