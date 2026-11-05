# Local model plan for this Mac

Checked on November 5, 2026. The development machine reports an Apple M5 MacBook Pro with 24 GB unified memory and macOS 26.6.2.

## Recommendation

Start with [`Qwen/Qwen3-8B-MLX-4bit`](https://huggingface.co/Qwen/Qwen3-8B-MLX-4bit) as the default research model. It is an MLX model from Qwen, uses the Apache 2.0 license, and is supported by `mlx-lm`. Apple's MLX benchmark on an M5 MacBook Pro with the same 24 GB memory configuration measured 5.61 GB for Qwen3-8B-4bit. That leaves more room for macOS, the dashboard/database, and the inference context cache than the larger alternatives.

The research task is short evidence synthesis for only the strongest deterministic candidates, so this model size is a reasonable first quality/speed/memory balance. The model may explain supplied evidence; deterministic code remains responsible for all calculations and risk checks.

## Alternatives to benchmark

| Model | Apple measured inference memory | Use |
| --- | ---: | --- |
| Qwen3-8B-MLX-4bit | 5.61 GB | Default starting point; leaves substantial unified-memory headroom. |
| Qwen3-14B-MLX-4bit | 9.16 GB | Quality comparison if 8B summaries are weak. |
| GPT-OSS 20B MXFP4 Q4 | 12.08 GB | Compare with the original brief's preferred model; likely workable, but leaves less headroom for long prompts and concurrent apps. |

Apple reports all three benchmarked on an M5 MacBook Pro with 24 GB unified memory. Its M5 Neural Accelerator path for MLX requires macOS 26.2 or later; this Mac is on 26.6.2. Context length still affects memory use, so start with concise evidence bundles and a modest generation cap rather than sending whole filings or transcripts.

## Running the local server

Install MLX LM in an isolated tool environment and serve the selected model on loopback:

```sh
uv tool install mlx-lm
mlx_lm.server --model Qwen/Qwen3-8B-MLX-4bit --host 127.0.0.1 --port 8080
```

The first run downloads model weights from Hugging Face. Thetahawk's optional narrative client uses the local OpenAI-compatible chat-completions endpoint at `http://127.0.0.1:8080/v1`; it rejects non-loopback hosts so evidence is not sent to a remote inference service.

To compare GPT-OSS, stop the server and start it with:

```sh
mlx_lm.server --model mlx-community/gpt-oss-20b-MXFP4-Q4 --host 127.0.0.1 --port 8080
```

Model memory figures are Apple's published benchmark measurements, not guarantees for every prompt length or workload. Before selecting a long-term model, compare both against the same fixed set of historical research cases for factual grounding, useful contrary evidence, latency, and memory pressure. The model must not provide numeric calculations or make trade decisions.

## Sources

- [Apple ML Research: Exploring LLMs with MLX and the Neural Accelerators in the M5 GPU](https://machinelearning.apple.com/research/exploring-llms-mlx-m5) — benchmark configuration, measured memory, and macOS requirement.
- [Qwen Qwen3-8B MLX model card](https://huggingface.co/Qwen/Qwen3-8B-MLX-4bit) — MLX LM usage and model license.
- [MLX LM documentation](https://github.com/ml-explore/mlx-lm) — local generation, server, supported models, and context-cache controls.
- [MLX Community GPT-OSS 20B MXFP4 Q4 model card](https://huggingface.co/mlx-community/gpt-oss-20b-MXFP4-Q4) — MLX format and model usage.
