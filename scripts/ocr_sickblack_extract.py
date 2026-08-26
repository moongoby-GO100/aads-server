import asyncio
import base64
import json
import os
import sys
import traceback

import pypdfium2 as pdfium

OUT_DIR = "/tmp/sickblack_render"
os.makedirs(OUT_DIR, exist_ok=True)

FILE_CORP = "/tmp/sickblack/9df77095-a449-4210-aa43-30de5d96869f.pdf"
FILE_LEASE = "/tmp/sickblack/ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf"

MODEL = "claude-sonnet-4-6"


def render_pdf(path, prefix, scale=2.5):
    doc = pdfium.PdfDocument(path)
    out_paths = []
    for i in range(len(doc)):
        page = doc[i]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        out_path = os.path.join(OUT_DIR, f"{prefix}_p{i+1}.png")
        pil_image.save(out_path)
        out_paths.append(out_path)
    doc.close()
    return out_paths


def b64_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def build_content(image_paths, prompt_text):
    content = []
    for p in image_paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64_image(p),
            },
        })
    content.append({"type": "text", "text": prompt_text})
    return content


async def call_anthropic(content, max_tokens=4000):
    sys.path.insert(0, "/app")
    from app.core.auth_provider import create_anthropic_client

    tokens_env = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_2"]
    last_err = None
    for env_name in tokens_env:
        key = os.environ.get(env_name)
        if not key:
            continue
        try:
            client = create_anthropic_client(token=key)
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            text = resp.content[0].text
            return {"ok": True, "source": f"anthropic:{env_name}", "text": text}
        except Exception as e:
            last_err = f"{env_name}: {repr(e)}"
            continue

    # LiteLLM/Gemini fallback
    try:
        from app.core.auth_provider import get_litellm_config
    except Exception:
        get_litellm_config = None

    try:
        import openai
        base_url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/") + "/v1"
        api_key = os.environ.get("LITELLM_MASTER_KEY", "sk-litellm")
        oai_client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        # rebuild content for OpenAI-style image_url
        oai_content = []
        for block in content:
            if block["type"] == "image":
                b64 = block["source"]["data"]
                oai_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            else:
                oai_content.append({"type": "text", "text": block["text"]})
        resp = await oai_client.chat.completions.create(
            model="gemini-2.5-flash",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": oai_content}],
        )
        text = resp.choices[0].message.content
        return {"ok": True, "source": "litellm:gemini-2.5-flash", "text": text}
    except Exception as e:
        last_err = f"{last_err} | litellm: {repr(e)}"

    return {"ok": False, "error": last_err}


CORP_PROMPT = """당신은 한국 법인등기부등본을 판독하는 전문가입니다. 첨부된 이미지는 스캔된 법인등기부등본 페이지들입니다(총 페이지 수는 이미지 개수와 같음).
아래 항목을 정확히 읽어서 JSON으로만 답하십시오. 읽을 수 없는 항목은 반드시 "미판독"으로 표기하고 추측하지 마십시오.

{
  "상호": "",
  "본점소재지": "",
  "등기번호": "",
  "법인등록번호": "",
  "1주의금액": "",
  "발행주식의총수": "",
  "자본금의액": "",
  "목적_개수": 0,
  "목적_요약": ["..."],
  "임원": {
    "이사_수": 0,
    "이사_명단": [
      {"성명": "", "직위": "", "취임일자": "", "임기만료예정일": "", "비고": ""}
    ],
    "대표이사_성명": "",
    "대표이사_주소": "",
    "감사_유무": "",
    "감사_명단": [
      {"성명": "", "취임일자": "", "임기만료예정일": ""}
    ]
  },
  "지점_유무": "",
  "마지막_등기변경일": "",
  "판독_불확실_항목": ["표나 항목이 흐릿하거나 확신이 없는 경우 여기에 기재"]
}

주의:
- 이사/감사 각 인물의 성명, 취임일자, 임기만료예정일을 표에서 정확히 대조하여 누락 없이 기재하십시오.
- 등기부등본에 나온 실제 텍스트 그대로 옮기고 임의로 요약/변형하지 마십시오.
- JSON 외의 다른 텍스트(설명, 코드블록 표시 등)를 출력하지 마십시오.
"""

LEASE_PROMPT = """당신은 한국 부동산 임대차계약서를 판독하는 전문가입니다. 첨부된 이미지는 스캔된 임대차계약서 페이지입니다.
아래 항목을 정확히 읽어서 JSON으로만 답하십시오. 읽을 수 없는 항목은 반드시 "미판독"으로 표기하고 추측하지 마십시오.

{
  "부동산_소재지": "",
  "임대인": {"성명또는상호": "", "주소": "", "연락처": ""},
  "임차인": {"성명또는상호": "", "주소": "", "연락처": ""},
  "계약일": "",
  "임대차기간_시작일": "",
  "임대차기간_종료일": "",
  "보증금": "",
  "월세": "",
  "관리비": "",
  "용도": "",
  "면적": "",
  "특약사항": ["..."],
  "판독_불확실_항목": ["표나 항목이 흐릿하거나 확신이 없는 경우 여기에 기재"]
}

주의:
- 문서에 나온 실제 텍스트 그대로 옮기고 임의로 요약/변형하지 마십시오.
- JSON 외의 다른 텍스트(설명, 코드블록 표시 등)를 출력하지 마십시오.
"""


async def main():
    print("=== Rendering corp registry pages ===", flush=True)
    corp_pages = render_pdf(FILE_CORP, "corp", scale=2.5)
    print(json.dumps(corp_pages, ensure_ascii=False), flush=True)

    print("=== Rendering lease contract page ===", flush=True)
    lease_pages = render_pdf(FILE_LEASE, "lease", scale=2.5)
    print(json.dumps(lease_pages, ensure_ascii=False), flush=True)

    print("=== Calling vision model for corp registry ===", flush=True)
    corp_content = build_content(corp_pages, CORP_PROMPT)
    corp_result = await call_anthropic(corp_content, max_tokens=4000)
    print("CORP_RESULT_SOURCE:", corp_result.get("source") or corp_result.get("error"), flush=True)
    print("CORP_RESULT_BEGIN", flush=True)
    print(corp_result.get("text", ""), flush=True)
    print("CORP_RESULT_END", flush=True)

    print("=== Calling vision model for lease contract ===", flush=True)
    lease_content = build_content(lease_pages, LEASE_PROMPT)
    lease_result = await call_anthropic(lease_content, max_tokens=3000)
    print("LEASE_RESULT_SOURCE:", lease_result.get("source") or lease_result.get("error"), flush=True)
    print("LEASE_RESULT_BEGIN", flush=True)
    print(lease_result.get("text", ""), flush=True)
    print("LEASE_RESULT_END", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
