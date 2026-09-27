"""Environment-configured adapter for the paired Laya GGUF encoder and head."""
from __future__ import annotations

import os
import json
import urllib.request
from pathlib import Path

from runtime.laya_gguf import LayaGGUF


def _required_path(name: str) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must point to the Laya GGUF asset")
    path = Path(value).expanduser()
    if not path.is_file() and name != "FORGE_LAYA_TOKENIZER_DIR":
        raise FileNotFoundError(f"{name} does not exist: {path}")
    if name == "FORGE_LAYA_TOKENIZER_DIR" and not (path / "tokenizer.json").is_file():
        raise FileNotFoundError(f"tokenizer.json is missing under {path}")
    return path


class LayaGGUFBackend:
    """Run the encoder through llama.cpp and the trained decision head in PyTorch."""

    def __init__(self) -> None:
        self.base_url = os.getenv("FORGE_LAYA_LLM_URL", "http://127.0.0.1:8099").strip().rstrip("/")
        timeout = float(os.getenv("FORGE_LAYA_LLM_TIMEOUT", "120"))
        encoder = _required_path("FORGE_LAYA_ENCODER_GGUF")
        head = _required_path("FORGE_LAYA_HEAD_GGUF")
        tokenizer_dir = _required_path("FORGE_LAYA_TOKENIZER_DIR")
        self._ensure_server(encoder)
        self._agent = LayaGGUF(
            encoder_gguf=encoder,
            head_gguf=head,
            tokenizer_dir=tokenizer_dir,
            base_url=self.base_url,
            timeout=timeout,
        )

    def _ensure_server(self, expected_encoder: Path) -> None:
        try:
            req = urllib.request.Request(self.base_url + "/props", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as response:
                if response.status != 200:
                    raise RuntimeError(f"llama.cpp /props returned HTTP {response.status}")
                props = json.loads(response.read().decode("utf-8"))
            loaded = props.get("model_path") or props.get("model_alias")
            if not loaded:
                raise RuntimeError("llama.cpp /props did not identify the loaded encoder model")
            if os.path.normcase(os.path.abspath(loaded)) != os.path.normcase(os.path.abspath(expected_encoder)):
                raise RuntimeError(
                    f"llama.cpp is serving {loaded!r}, expected encoder {str(expected_encoder)!r}"
                )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("llama.cpp is serving"):
                raise
            raise RuntimeError(
                f"Laya GGUF llama.cpp server is unavailable at {self.base_url}; "
                "start the configured encoder GGUF server first and ensure it loaded the configured file"
            ) from exc

    def predict(self, query, questions):
        return self._agent.predict(query, questions=questions)

    def system_one(self, state, questions):
        return self._agent.system_one(state, questions=questions)
