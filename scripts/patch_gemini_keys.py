"""litellm-config.yaml에 GEMINI_API_KEY_2 로드밸런싱 엔트리 추가."""
import pathlib

CONFIG = pathlib.Path("/root/aads/aads-server/litellm-config.yaml")
text = CONFIG.read_text()

if "GEMINI_API_KEY_2" in text:
    print("이미 GEMINI_API_KEY_2 엔트리 존재 — 스킵")
    raise SystemExit(0)

GEMINI_MODELS = [
    ("gemini-2.5-flash", "gemini/gemini-2.5-flash"),
    ("gemini-2.5-flash-lite", "gemini/gemini-2.5-flash-lite"),
    ("gemini-2.5-pro", "gemini/gemini-2.5-pro"),
    ("gemini-2.5-flash-image", "gemini/gemini-2.5-flash-image"),
    ("gemini-3-pro-preview", "gemini/gemini-3-pro-preview"),
    ("gemini-3-flash-preview", "gemini/gemini-3-flash-preview"),
    ("gemini-3.1-pro-preview", "gemini/gemini-3.1-pro-preview"),
    ("gemini-3.1-flash-lite-preview", "gemini/gemini-3.1-flash-lite-preview"),
    ("gemma-3-27b-it", "gemini/gemma-3-27b-it"),
    ("gemini-flash-lite", "gemini/gemini-2.5-flash-lite"),
    ("gemini-flash", "gemini/gemini-2.5-flash"),
    ("gemini-pro", "gemini/gemini-2.5-pro"),
]

block = "# ── GEMINI_API_KEY_2 로드밸런싱 (aads 계정) ──\n"
for name, model in GEMINI_MODELS:
    block += f"- model_name: {name}\n"
    block += f"  litellm_params:\n"
    block += f"    model: {model}\n"
    block += f"    api_key: os.environ/GEMINI_API_KEY_2\n"

anchor = "- model_name: deepseek-chat\n"
text = text.replace(anchor, block + anchor)

CONFIG.write_text(text)
print(f"완료: {len(GEMINI_MODELS)}개 모델 KEY_2 엔트리 추가")
