import base64
import json
import os
import urllib.request

from PIL import Image

IMG_DIR = "/app/docs/law_extract/img"
OUT_DIR = "/app/docs/law_extract"
BASE = "/root/aads/uploads/chat/files/efccec7c-0788-4564-a2cf-265c63d075f0"

# 사업자등록증 webp -> jpg
src = os.path.join(BASE, "e79320be-8236-426e-ac4b-d3e607c4f2ab.webp")
biz_jpg = os.path.join(IMG_DIR, "saeobja.jpg")
if os.path.exists(src):
    im = Image.open(src).convert("RGB")
    im.save(biz_jpg, "JPEG", quality=85)
    print("saeobja jpg", os.path.getsize(biz_jpg))

URL = "http://aads-litellm:4000/v1/chat/completions"
KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")
MODEL = "gemini-2.5-pro"

PROMPT = (
    "이 이미지는 한국 공식 문서 스캔본이다. 이미지에 보이는 모든 텍스트를 "
    "누락 없이 원문 그대로 한국어로 그대로 옮겨 적어라. 표는 표 형태를 유지하고, "
    "날짜/숫자/주소/성명/등록번호는 정확히 그대로 적어라. 해설이나 요약은 하지 마라."
)


def ocr(path):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }
        ],
        "max_tokens": 8000,
        "temperature": 0,
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"]


targets = []
for i in (1, 2, 3):
    p = os.path.join(IMG_DIR, "deungibu_p%02d.jpg" % i)
    if os.path.exists(p):
        targets.append(("deungibu_p%02d" % i, p))
if os.path.exists(biz_jpg):
    targets.append(("saeobja", biz_jpg))

for name, path in targets:
    try:
        txt = ocr(path)
        op = os.path.join(OUT_DIR, "ocr_%s.txt" % name)
        with open(op, "w", encoding="utf-8") as f:
            f.write(txt)
        print("OK", name, len(txt))
    except Exception as e:
        print("ERR", name, repr(e))
