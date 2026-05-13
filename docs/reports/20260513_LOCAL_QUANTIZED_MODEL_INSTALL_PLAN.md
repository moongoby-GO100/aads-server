# RTX 3060 12GB Local Quantized Model Install Plan

Date: 2026-05-13 KST

## Summary

CEO PC target is RTX 3060 12GB + 32GB RAM. Install order must be staged:

1. Baseline models that should run reliably and expose in AADS chat/tools.
2. Heavy comparison models that may run through quantization/offload but must not become defaults before benchmark.
3. Media and document pipelines that need a non-Ollama runtime such as ComfyUI, Diffusers, PaddleOCR, Transformers, or ONNX Runtime.

## AADS Integration Status

| Area | Runtime | AADS bridge status | Default policy |
|---|---|---|---|
| Text LLM | Ollama GGUF | `pc_ollama` direct PC Agent route | Enable after benchmark |
| Vision-language | Ollama GGUF | `pc_ollama` direct PC Agent route | Enable after benchmark |
| Embedding/rerank | Transformers / sentence-transformers | `local_embedding` / `local_rerank` queue manager | Utility route, not chat default |
| OCR/document | PaddleOCR / Tesseract / Transformers | `local_document` queue manager | Tool route |
| Image generation | ComfyUI / Diffusers | `local_image` async media job | Tool route |
| Video generation | ComfyUI / Diffusers | `local_video` async media job | Async tool route |
| STT/TTS/audio | Whisper / Piper / Transformers | `local_audio` queue manager | Tool route |
| Music/audio generation | Stable Audio Open / AudioCraft class runtimes | `local_music` async media job | Experiment only |
| 3D generation | Hunyuan3D / ComfyUI | `local_3d` async media job | Experiment only |

## Implementation Added

- `scripts/local_model_install_queue.json` is the canonical queue.
- `app/services/local_model_manager.py` lists queue items, reports PC Agent capability/lease state, and routes one install/test job at a time.
- `pc_agent/commands/local_models.py` adds safe PC Agent handlers:
  - `local_model_queue_status`
  - `local_model_install_test`
  - `local_model_media_job`
- `local_model_install` and `local_media_job` PC Agent lease concurrency is capped at 1.
- `generate_music`, `generate_three_d_asset`, `media_job_status`, `local_model_queue_status`, and `local_model_install_test` are exposed as chat/tool calls.
- `image`, `edit_image`, `video`, `music`, and `model_3d` local media calls are async job style. They return queued/prepared metadata and do not become default chat routes.

## Install Queue

| Priority | Field | Model | Runtime | AADS use |
|---:|---|---|---|---|
| 1 | Text | `gemma4:e4b` | Ollama | local draft, private summary, smoke test |
| 1 | Text | `qwen3:4b`, `qwen3:8b` | Ollama | Korean/code utility, local fallback |
| 1 | Vision | `qwen2.5vl:3b` | Ollama | image QA/OCR smoke |
| 1 | STT | `openai/whisper-large-v3-turbo` | Transformers | local transcription |
| 1 | OCR | PaddleOCR-VL + Tesseract 5 | PaddleOCR/Tesseract | PDF/image text extraction |
| 2 | Text | `qwen3:14b`, `gemma4:26b` | Ollama | quality comparison only |
| 2 | Embedding | `Qwen3-Embedding-0.6B`, `BAAI/bge-m3` | Transformers | RAG/search replacement test |
| 2 | Image | FLUX.2 klein 4B, Qwen-Image, Z-Image-Turbo | ComfyUI/Diffusers | local image generation/edit POC |
| 2 | Video | Wan2.2 TI2V 5B, LTX-Video | ComfyUI/Diffusers | short local video POC |
| 3 | Rerank | Qwen3-Reranker 0.6B | Transformers | RAG precision boost |
| 3 | 3D | Hunyuan3D 2.1 | ComfyUI/Diffusers | image-to-3D asset experiment |
| 3 | Music | Stable Audio Open | Transformers/Diffusers | experiment only; license review before business use |

## Acceptance Criteria

Each installed model must record:

- install status and disk size
- first-load time
- steady-state tokens/sec or seconds/job
- GPU VRAM peak if available
- one Korean business prompt result
- AADS route/tool id
- license note
- pass/fail decision

## Guardrails

- Do not install all large media weights in parallel.
- Keep default chat route on cloud/frontier models until local benchmarks pass.
- Use local models first for privacy-sensitive drafts, OCR, transcription, and cheap batch jobs.
- Keep image/video/music/3D behind async job tools, not the main chat model selector.
- Do not claim installation completed while PC Agent is offline; report queued/prepared separately from installed/tested.

## API And Tools

- REST:
  - `GET /api/v1/local-models/queue`
  - `GET /api/v1/local-models/status`
  - `POST /api/v1/local-models/run`
- Chat/tool names:
  - `local_model_queue_status`
  - `local_model_install_test`
  - `generate_image` with `provider=pc_local` for `local_image`
  - `generate_video` with `provider=pc_local` for `local_video`
  - `generate_music`
  - `generate_three_d_asset`
  - `media_job_status`

## Official Sources Checked

- Ollama model library: `https://ollama.com/models`
- Qwen3 Embedding: `https://github.com/QwenLM/Qwen3-Embedding`
- Qwen-Image: `https://github.com/QwenLM/Qwen-Image`
- FLUX.2: `https://github.com/black-forest-labs/flux2`
- Wan2.2: `https://github.com/Wan-Video/Wan2.2`
- PaddleOCR-VL: `https://huggingface.co/PaddlePaddle/PaddleOCR-VL`
- Whisper large-v3-turbo: `https://huggingface.co/openai/whisper-large-v3-turbo`
- Hunyuan3D 2.1: `https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1`
- Stable Audio Open: `https://huggingface.co/stabilityai/stable-audio-open-1.0`
