"""Run a Laya decision model on top of llama.cpp.

llama.cpp's ``modern-bert`` graph runs the 22-layer mmBERT encoder, and the
decision head (2 transformer layers, the option-marker scorer and the act head)
runs here in PyTorch from the companion head GGUF. Encoder hidden states come
from a llama-server started with ``--embeddings --pooling none``.

    llama-server -m laya-multilingual-f16.gguf --embeddings --pooling none \
                 --ctx-size 1024 --port 8099

The public API mirrors the official ``laya`` package's ``Agent``.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import torch
from tokenizers import Tokenizer

from .gguf_weights import load_head_gguf

QTYPES = {"choice": 0, "score": 1, "noul": 2}
QTYPE_NAMES = {v: k for k, v in QTYPES.items()}


def serialize_state(state: Union[str, dict, list]) -> str:
    if isinstance(state, str):
        return state
    return json.dumps(state, ensure_ascii=False)


def render_criterion(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "), default=str)


def render_options(q: Dict) -> List[str]:
    t, crit = q["t"], q.get("crit")
    if t == "choice":
        return [k if v is None or v == "" else "%s: %s" % (k, render_criterion(v)) for k, v in crit.items()]
    if t == "score":
        return ["level %d: %s" % (i, render_criterion(c)) for i, c in enumerate(crit)]
    crit = crit or {}
    false_crit, true_crit = crit.get("false"), crit.get("true")
    return [
        "false: " + (render_criterion(false_crit) if false_crit not in (None, "") else "no, the statement does not hold"),
        "true: " + (render_criterion(true_crit) if true_crit not in (None, "") else "yes, the statement holds"),
    ]


def confidence_from_probs(p: np.ndarray, k: int) -> float:
    if k < 2:
        return 1.0
    p = np.asarray(p[:k], dtype=np.float64)
    ent = -(p * np.log(np.clip(p, 1e-12, 1.0))).sum()
    import math
    return float(np.clip(1.0 - ent / math.log(k), 0.0, 1.0))


def temp_bucket(qtype: int, k: int) -> str:
    size = "2" if k <= 2 else "3-5" if k <= 5 else "6-10" if k <= 10 else "11+"
    return "%s:%s" % (QTYPE_NAMES[int(qtype)], size)


class LayaGGUF:
    """Laya decision model driven by llama.cpp for the encoder."""

    def __init__(
        self,
        encoder_gguf: Union[str, Path],
        head_gguf: Union[str, Path],
        tokenizer_dir: Union[str, Path],
        base_url: str = "http://127.0.0.1:8099",
        timeout: float = 120.0,
    ):
        self.encoder_gguf = Path(encoder_gguf)
        self.head_gguf = Path(head_gguf)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        w, head_cfg = load_head_gguf(self.head_gguf)
        self.w = w
        self.hcfg = head_cfg
        self.cfg = {
            "max_len": head_cfg.get("max_len", 1024),
            "head_max_len": head_cfg.get("head_max_len", 256),
            "head_layers": head_cfg.get("head_layers", 2),
        }
        self.temperature = head_cfg.get("temperature", [1.0, 1.0, 1.0])
        self.temperature_by_options = head_cfg.get("temperature_by_options", {})
        st = head_cfg["special_tokens"]
        self.cls_id = st["cls"]
        self.sep_id = st["sep"]
        self.mask_id = st["mask"]
        self.pad_id = st["pad"]

        self.tok = Tokenizer.from_file(str(Path(tokenizer_dir) / "tokenizer.json"))
        self.n_head = 12
        self.d = 768
        self.dh = self.d // self.n_head

    # -- sequence construction (identical to laya.common.build_sequence) -----
    def build_sequence(self, state, q: Dict, option_order=None, truncate_left: bool = False):
        mask_tok = "<mask>"
        opts = render_options(q)
        order = option_order if option_order is not None else list(range(len(opts)))
        ins = str(q["ins"]).replace(mask_tok, " ")
        enc = lambda s: self.tok.encode(s, add_special_tokens=False).ids
        head_ids = enc("%s question: %s" % (q["t"], ins))
        opt_ids = []
        for i in order:
            opt_ids.append([self.mask_id] + enc(" " + opts[i].replace(mask_tok, " "))[:48])
        head_max_len = self.cfg["head_max_len"]
        max_len = self.cfg["max_len"]
        opt_budget = head_max_len - sum(len(o) for o in opt_ids)
        if opt_budget < 16:
            per = max(4, (head_max_len - 16) // max(1, len(opt_ids)))
            opt_ids = [o[:per] for o in opt_ids]
            opt_budget = head_max_len - sum(len(o) for o in opt_ids)
        head_ids = head_ids[: max(8, opt_budget)]
        ids = [self.cls_id] + head_ids + [self.sep_id]
        markers = []
        for o in opt_ids:
            markers.append(len(ids))
            ids.extend(o)
        ids.append(self.sep_id)
        room = max(0, max_len - len(ids) - 1)
        st = enc(serialize_state(state).replace(mask_tok, " "))
        st = st[-room:] if truncate_left else st[:room]
        ids = ids + st + [self.sep_id]
        return ids[:max_len], [m for m in markers if m < max_len]

    # -- llama.cpp encoder --------------------------------------------------
    def encode(self, ids: Sequence[int]) -> np.ndarray:
        payload = json.dumps({"input": list(ids), "embd_normalize": -1}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/embeddings", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"llama-server error: {data['error']}")
        if not isinstance(data, list) or not data or "embedding" not in data[0]:
            raise RuntimeError(f"unexpected /embeddings response: {str(data)[:400]}")
        arr = np.asarray(data[0]["embedding"], dtype=np.float32)
        return arr.reshape(len(ids), -1)

    # -- decision head ------------------------------------------------------
    def _norm(self, x, weight, bias):
        return torch.nn.functional.layer_norm(x, (self.d,), weight, bias, 1e-5)

    def decision_head(self, h: np.ndarray, marker_pos: Sequence[int], qtype: int):
        w = self.w
        x = torch.from_numpy(np.ascontiguousarray(h)).float()[None]
        x = x + w["type_emb.weight"][qtype][None, None]

        pad = torch.zeros(1, x.shape[1], dtype=torch.bool)
        src_key_padding_mask = pad
        for i in range(self.cfg["head_layers"]):
            p = f"head.layers.{i}."
            n = self._norm(x, w[p + "norm1.weight"], w[p + "norm1.bias"])
            q, k, v = torch.nn.functional.linear(
                n, w[p + "self_attn.in_proj_weight"], w[p + "self_attn.in_proj_bias"]
            ).chunk(3, dim=-1)
            B, T, _ = q.shape
            reshape = lambda t: t.view(B, T, self.n_head, self.dh).transpose(1, 2)
            q, k, v = reshape(q), reshape(k), reshape(v)
            att = (q @ k.transpose(-1, -2) / (self.dh ** 0.5))
            att = att.masked_fill(src_key_padding_mask[:, None, None, :], float("-inf"))
            att = att.softmax(-1)
            o = (att @ v).transpose(1, 2).reshape(B, T, self.d)
            o = torch.nn.functional.linear(o, w[p + "self_attn.out_proj.weight"], w[p + "self_attn.out_proj.bias"])
            x = x + o
            n = self._norm(x, w[p + "norm2.weight"], w[p + "norm2.bias"])
            n = torch.nn.functional.linear(n, w[p + "linear1.weight"], w[p + "linear1.bias"])
            n = torch.nn.functional.gelu(n)
            n = torch.nn.functional.linear(n, w[p + "linear2.weight"], w[p + "linear2.bias"])
            x = x + n

        idx = torch.tensor(list(marker_pos), dtype=torch.long)
        m = x[0].index_select(0, idx)
        s = self._norm(m, w["scorer.0.weight"], w["scorer.0.bias"])
        s = torch.nn.functional.linear(s, w["scorer.1.weight"], w["scorer.1.bias"])
        s = torch.nn.functional.gelu(s)
        logits = torch.nn.functional.linear(s, w["scorer.3.weight"], w["scorer.3.bias"]).squeeze(-1)

        p = torch.softmax(logits.detach(), -1).numpy()
        k_opt = max(2, len(marker_pos))
        ent = float(-(p * np.log(np.clip(p, 1e-9, None))).sum() / np.log(k_opt))
        order = np.argsort(-p)
        top1, top2 = p[order[0]], p[order[1]]
        feats = torch.tensor([top1, top1 - top2, ent, k_opt / 255.0], dtype=torch.float32)
        pooled = x[0, 0]
        a = self.w["act_head.0.weight"] @ torch.cat([pooled, feats]) + self.w["act_head.0.bias"]
        a = torch.nn.functional.gelu(a)
        a = self.w["act_head.2.weight"] @ a + self.w["act_head.2.bias"]
        return logits.detach().numpy(), a.detach().numpy()

    # -- public API (mirrors laya.Agent.system_one) -------------------------
    @staticmethod
    def _to_internal(qdef: Dict) -> Dict:
        t = qdef["type"]
        crit = qdef.get("criteria")
        if t == "choice" and isinstance(crit, list):
            crit = {c: None for c in crit}
        ins = qdef["instructions"]
        if not isinstance(ins, str):
            ins = json.dumps(ins)
        return {"t": t, "ins": ins, "crit": crit}

    def predict(self, state: Union[str, dict, list], questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ids_list = list(questions.keys())
        items = []
        for qid in ids_list:
            q = self._to_internal(questions[qid])
            seq, markers = self.build_sequence(state, q)
            if len(markers) != len(render_options(q)):
                raise ValueError(f"question {qid!r} options exceed head_max_len={self.cfg['head_max_len']}")
            items.append({"ids": seq, "markers": markers, "qtype": QTYPES[q["t"]]})

        answers: Dict[str, Any] = {}
        n_tokens = 0
        for qid, it in zip(ids_list, items):
            q = self._to_internal(questions[qid])
            h = self.encode(it["ids"])
            n_tokens += len(it["ids"])
            logits, act = self.decision_head(h, it["markers"], it["qtype"])
            k = len(it["markers"])
            qt = QTYPES[q["t"]]
            t_scale = self.temperature_by_options.get(temp_bucket(qt, k), self.temperature[qt])
            z = logits[:k] / max(1e-3, float(t_scale))
            p = np.exp(z - z.max())
            p = p / p.sum()

            conf = round(confidence_from_probs(p, k), 4)
            act_p = torch.softmax(torch.from_numpy(act).float(), -1).numpy()
            ext = {"act_probability": round(float(act_p[0]), 4)}

            if q["t"] == "choice":
                keys = list(q["crit"].keys())
                answers[qid] = {
                    "type": "choice",
                    "choice": keys[int(p.argmax())],
                    "probabilities": {kk: round(float(v), 4) for kk, v in zip(keys, p)},
                    "confidence": conf,
                    "action": ext,
                }
            elif q["t"] == "score":
                answers[qid] = {
                    "type": "score",
                    "score": round(float((np.arange(k) * p).sum()), 4),
                    "legend": {str(i): c for i, c in enumerate(q["crit"])},
                    "probabilities": {str(i): round(float(v), 4) for i, v in enumerate(p)},
                    "confidence": conf,
                    "action": ext,
                }
            else:
                answers[qid] = {
                    "type": "noul",
                    "noul": round(float(p[1]), 4),
                    "confidence": round(max(float(p[1]), 1.0 - float(p[1])), 4),
                    "action": ext,
                }

        return {
            "model": "laya-multilingual (gguf/llama.cpp)",
            "answers": answers,
            "n_tokens": n_tokens,
            "usage": {"input_tokens": n_tokens, "output_tokens": 0},
        }

    def system_one(self, state: Union[str, dict, list], questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Compatibility alias for callers using the official Laya Agent API."""
        return self.predict(state, questions)
