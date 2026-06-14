import os
import base64
import json
import urllib.request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

with open("/app/app/static/media/kakaotalk_photo.jpg", "rb") as f:
    img_bytes = f.read()
img_b64 = base64.b64encode(img_bytes).decode()

payload = {
    "contents": [
        {
            "parts": [
                {
                    "text": "Remove the necktie from this person's photo. Keep everything else exactly the same. Output the edited image.",
                },
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": img_b64,
                    }
                }
            ]
        }
    ],
    "generationConfig": {
        "responseModalities": ["IMAGE", "TEXT"],
    }
}

for model in ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image", "gemini-3.1-flash-image"]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        candidates = result.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                # handle both snake_case and camelCase
                inline = part.get("inline_data") or part.get("inlineData")
                if inline:
                    img_data = base64.b64decode(inline["data"])
                    mime = inline.get("mime_type") or inline.get("mimeType", "image/jpeg")
                    ext = "png" if "png" in mime else "jpg"
                    out_path = f"/app/app/static/media/kakaotalk_no_tie.{ext}"
                    with open(out_path, "wb") as f:
                        f.write(img_data)
                    print(f"OK: saved {len(img_data)} bytes -> {out_path} using {model}")
                    exit(0)
        print(f"no image from {model}")
    except Exception as e:
        print(f"{model} error: {e}")
