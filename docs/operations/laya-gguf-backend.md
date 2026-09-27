# Laya GGUF backend

The project can use the paired Laya model files through the `gguf` backend. The
encoder runs in llama.cpp; the companion decision head runs in PyTorch. The head
file is not an encoder and must not be passed to `llama-server` as the model.

## Required files and settings

Keep these files together outside the repository, as they are in `F:\laya`:

- `laya-multilingual-f16.gguf` — encoder loaded by llama.cpp
- `laya-multilingual-f16-head.gguf` — trained decision head read by Python
- `tokenizer.json` — tokenizer read by Python

Set `FORGE_LAYA_ENCODER_GGUF`, `FORGE_LAYA_HEAD_GGUF`, and
`FORGE_LAYA_TOKENIZER_DIR` in `.env`. Set `FORGE_LAYA_BACKEND=gguf` and
`FORGE_LAYA_LLM_URL=http://127.0.0.1:8099`.

Install project dependencies after updating the checkout:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Start and verify llama.cpp

When starting TUI with `FORGE_LAYA_TUI_AUTOSTART=on`, it checks whether the
configured server is already running. If it is not, set
`FORGE_LAYA_LLAMA_SERVER_EXE` to the matching SYCL build's `llama-server.exe`;
the TUI launches it in the background with the encoder GGUF and configured GPU
layer count, then preloads the Python tokenizer and decision head. Chat input is
disabled only during this startup warm-up. The TUI closes a server it started
when the TUI exits, and leaves a server that was already running alone.

Automatic startup only binds to a loopback address by default. If the configured
`FORGE_LAYA_LLM_URL` uses a non-loopback host, startup is refused unless
`FORGE_LAYA_TUI_ALLOW_REMOTE_BIND=on` is explicitly set. llama.cpp's server has
no authentication configured here, so enabling remote binding exposes its
inference endpoints to clients that can reach that interface. Restrict access
with the host firewall and use a trusted network; do not bind `0.0.0.0` on an
untrusted network.

You can also start the encoder server manually in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File F:\laya\start_server.ps1 -Backend sycl -Port 8099
```

The project checks `/props` before loading the Laya head and verifies that the
server's reported `model_path` matches `FORGE_LAYA_ENCODER_GGUF`. A different
model on the same port is rejected. When the configured server or assets are
unavailable, Laya reports `gguf_unavailable` and the normal Agent path proceeds;
it does not silently load the previous Torch Laya checkpoint.

## Behavior and limits

`FORGE_LAYA_CONF` still gates whether a Laya result can short-circuit the main
Agent. Low confidence returns control to the main Agent. The paired GGUF contains
the multilingual Laya decision head, but it is not the project's separate
fine-tuned `forge_finetuned_5000_xpu_fixed_arith` checkpoint, and it does not
provide the separately trained multi-tool route head.

On the initial local smoke check, the live GGUF path loaded and produced answers
for Chinese weather and arithmetic requests. At the configured confidence
threshold (`0.85`), both fell back to the main Agent because their measured
confidence was low. Keep that fallback until a project-specific benchmark
supports changing the threshold.
