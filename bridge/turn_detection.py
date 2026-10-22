"""Lazy end-of-turn models, independent of FastAPI and bridge configuration.

Constructors never download weights or allocate a GPU. Text inference keeps
its lock through loading and scoring; an empty transcript returns 0.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
TURN_SAMPLES = 8 * 16000


def prepare_turn_audio(audio: np.ndarray) -> np.ndarray:
    """Keep the last eight seconds and left-pad short clips for Smart Turn."""
    audio = np.asarray(audio, dtype=np.float32)[-TURN_SAMPLES:]
    return np.pad(audio, (TURN_SAMPLES - len(audio), 0))


def turn_tokens(tokenizer) -> tuple[str, list[int]]:
    """Use the model's user-turn template, plus single-token punctuation.

    Only the first suffix token denotes a message boundary; whitespace or
    multi-token punctuation must not be counted as an end-of-turn score.
    """
    marker = "\u2063"
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": marker}], tokenize=False
    )
    prefix, suffix = rendered.split(marker, 1)
    suffix_ids = tokenizer.encode(suffix, add_special_tokens=False)
    if not suffix_ids:
        raise ValueError("The chat template must provide a user-turn end token")
    end_ids = [suffix_ids[0]]
    for character in "。？！?!":
        ids = tokenizer.encode(character, add_special_tokens=False)
        if len(ids) == 1:
            end_ids.append(ids[0])
    return prefix, sorted(set(end_ids))


class TurnDetector:
    """Smart Turn session, loaded once (startup warmup or first request)."""

    def __init__(self, model_path: Path, threads: int = 4) -> None:
        self.model_path = model_path
        self.threads = threads
        self._session = None
        self._fe = None
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._session is not None

    def load(self) -> None:
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor

            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.intra_op_num_threads = self.threads
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._fe = WhisperFeatureExtractor(chunk_length=8)
            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=so,
                providers=["CPUExecutionProvider"],
            )
            self.error = None
            logger.info(
                "Smart Turn loaded: %s (%d threads)", self.model_path.name, self.threads
            )

    def predict(self, audio: np.ndarray) -> float:
        """Probability that the speaker has finished (float32 mono 16 kHz)."""
        self.load()
        audio = prepare_turn_audio(audio)
        features = self._fe(
            audio,
            sampling_rate=16000,
            return_tensors="np",
            padding="max_length",
            max_length=TURN_SAMPLES,
            truncation=True,
            do_normalize=True,
        ).input_features.astype(np.float32)
        out = self._session.run(None, {"input_features": features})
        return float(np.asarray(out[0]).reshape(-1)[0])


class TextTurnJudge:
    """Small chat LLM that scores whether an unpunctuated transcript is a
    finished user message (loaded once, startup warmup or first request)."""

    def __init__(self, model_path: Path, device: str = "cpu") -> None:
        self.model_path = model_path
        self.device = device
        self._lm = None
        self._tok = None
        self._prefix = ""
        self._end_ids: list[int] = []
        self._lock = threading.Lock()
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        return self._lm is not None

    def load(self) -> None:
        if self._lm is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(self.model_path)
        prefix, end_ids = turn_tokens(tok)
        lm = AutoModelForCausalLM.from_pretrained(self.model_path, dtype=torch.bfloat16)
        self._tok, self._prefix, self._end_ids = tok, prefix, sorted(set(end_ids))
        self._lm = lm.to(self.device).eval()
        self.error = None
        logger.info(
            "Text turn judge loaded: %s on %s", self.model_path.name, self.device
        )

    def predict(self, text: str) -> float:
        """Probability that the user's message ends right after `text`."""
        if not text.strip():
            return 0.0  # 还没识别出字：继续听
        import torch

        with self._lock:
            self.load()
            ids = self._tok(
                self._prefix + text, return_tensors="pt", add_special_tokens=False
            ).input_ids
            with torch.inference_mode():
                logits = (
                    self._lm(ids.to(self.device), logits_to_keep=1)
                    .logits[0, -1]
                    .float()
                )
            return float(torch.softmax(logits, -1)[self._end_ids].sum())
