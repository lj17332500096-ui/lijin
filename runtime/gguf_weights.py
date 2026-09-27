"""Read the Laya decision-head companion GGUF into torch tensors."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import torch
from gguf import GGUFReader


def _to_tensor(t) -> torch.Tensor:
    """GGUFReader exposes tensors as 1-D numpy views; reshape to the ggml dim order."""
    arr = t.data
    # ggml stores dimensions reversed relative to torch
    shape = tuple(int(d) for d in t.shape)[::-1]
    pf = t.tensor_type
    if pf.name == "F32":
        a = arr.view(np.float32)
    elif pf.name == "F16":
        a = arr.view(np.float16).astype(np.float32)
    elif pf.name == "BF16":
        u = arr.view(np.uint16).astype(np.uint32) << 16
        a = u.view(np.float32)
    elif pf.name == "F64":
        a = arr.view(np.float64).astype(np.float32)
    else:
        raise NotImplementedError(f"unsupported tensor type {pf.name}")
    # GGUFReader memory maps are read-only; force owned storage before wrapping in torch.
    return torch.from_numpy(np.array(a.reshape(shape), copy=True, order="C")).float()


def load_head_gguf(path) -> Tuple[Dict[str, torch.Tensor], dict]:
    """Return (state_dict, head_config) for a Laya head GGUF."""
    reader = GGUFReader(str(path))
    weights: Dict[str, torch.Tensor] = {}
    for t in reader.tensors:
        weights[t.name] = _to_tensor(t)

    head_cfg = {}
    field = reader.fields.get("laya.head_config")
    if field is not None:
        # STRING field: the last part holds the utf-8 bytes
        part = field.parts[-1]
        head_cfg = json.loads(bytes(part.tolist()).decode("utf-8"))
    return weights, head_cfg
