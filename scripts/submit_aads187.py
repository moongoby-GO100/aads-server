"""AADS-187 Pipeline Runner 직접 제출 (JWT 인증 + 내부 포트 8080)."""
import sys
import urllib.request
import json

sys.path.insert(0, "/app")
from app.auth import create_token  # noqa: E402

INSTRUCTION = (
    "## TASK_ID: AADS-187\n"
    "## TITLE: 미디어 모델 DB 시드 + model_routing route_key 확장\n"
    "## PRIORITY: P1\n"
    "## SIZE: M\n"
    "## MODE: code_modify\n"
    "\n"
    "### 목적\n"
    "model_routing_preferences.route_key를 music/audio/runner_llm까지 확장하고,\n"
    "이미지/동영상/음악/음성/러너용 미디어 모델을 llm_models 및 model_routing_preferences에 시드한다.\n"
    "\n"
    "---\n"
    "\n"
    "### 수정 파일: app/api/llm_models.py\n"
    "\n"
    "#### [변경 A] ModelRoutingPreferenceInput.route_key 정규식 확장 (약 32번 줄)\n"
    "\n"
    "현재:\n"
    "    route_key: str = Field(..., pattern=r'^(image|edit_image|video|llm)$')\n"
    "변경:\n"
    "    route_key: str = Field(..., pattern=r'^(image|edit_image|video|llm|music|audio|runner_llm)$')\n"
    "\n"
    "---\n"
    "\n"
    "#### [변경 B] _seed_media_models() 함수 추가 + _ensure_model_routing_preferences_table() 수정\n"
    "\n"
    "_ensure_model_routing_preferences_table() 함수 정의 바로 위에 다음 함수를 삽입:\n"
    "\n"
    "async def _seed_media_models(conn) -> None:\n"
    "    \"\"\"미디어 모델 시드 — ON CONFLICT DO NOTHING으로 멱등 실행.\"\"\"\n"
    "    await conn.execute(\"\"\"\n"
    "        INSERT INTO llm_models\n"
    "          (provider, model_id, display_name, family, category,\n"
    "           execution_model_id, is_active, discovery_source)\n"
    "        VALUES\n"
    "          ('openai','dall-e-3','DALL-E 3','dall-e','media_image','dall-e-3',true,'manual'),\n"
    "          ('openai','dall-e-2','DALL-E 2','dall-e','media_image','dall-e-2',false,'manual'),\n"
    "          ('black-forest-labs','flux-1.1-pro','Flux 1.1 Pro','flux','media_image','flux-1.1-pro',true,'manual'),\n"
    "          ('black-forest-labs','flux-dev','Flux Dev','flux','media_image','flux-dev',false,'manual'),\n"
    "          ('stability','stable-diffusion-3-5','Stable Diffusion 3.5','stable-diffusion','media_image','stable-diffusion-3-5',true,'manual'),\n"
    "          ('stability','stable-diffusion-xl','Stable Diffusion XL','stable-diffusion','media_image','stable-diffusion-xl',false,'manual'),\n"
    "          ('google','imagen-4.0-generate-001','Imagen 4.0','imagen','media_image','imagen-4.0-generate-001',true,'manual'),\n"
    "          ('runway','gen-4-turbo','Runway Gen-4 Turbo','gen','media_video','gen-4-turbo',true,'manual'),\n"
    "          ('runway','gen-3-alpha','Runway Gen-3 Alpha','gen','media_video','gen-3-alpha',false,'manual'),\n"
    "          ('kling','kling-2.0','Kling 2.0','kling','media_video','kling-2.0',true,'manual'),\n"
    "          ('pika','pika-2.2','Pika 2.2','pika','media_video','pika-2.2',true,'manual'),\n"
    "          ('google','veo-3.0-generate-preview','Veo 3.0 Preview','veo','media_video','veo-3.0-generate-preview',true,'manual'),\n"
    "          ('suno','suno-v4','Suno v4','suno','media_music','suno-v4',true,'manual'),\n"
    "          ('udio','udio-3.0','Udio 3.0','udio','media_music','udio-3.0',true,'manual'),\n"
    "          ('meta','musicgen-large','MusicGen Large','musicgen','media_music','musicgen-large',false,'manual'),\n"
    "          ('meta','musicgen-melody','MusicGen Melody','musicgen','media_music','musicgen-melody',false,'manual'),\n"
    "          ('elevenlabs','eleven-v3','ElevenLabs v3','eleven','media_audio','eleven-v3',true,'manual'),\n"
    "          ('openai','tts-1-hd','OpenAI TTS HD','tts','media_audio','tts-1-hd',true,'manual'),\n"
    "          ('openai','tts-1','OpenAI TTS','tts','media_audio','tts-1',false,'manual'),\n"
    "          ('google','google-wavenet-tts','Google WaveNet TTS','wavenet','media_audio','google-wavenet-tts',false,'manual')\n"
    "        ON CONFLICT (provider, model_id) DO NOTHING\n"
    "    \"\"\")\n"
    "    await conn.execute(\"\"\"\n"
    "        INSERT INTO model_routing_preferences\n"
    "          (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_by)\n"
    "        VALUES\n"
    "          ('image','openai','dall-e-3',10,true,false,'DALL-E 3','system'),\n"
    "          ('image','black-forest-labs','flux-1.1-pro',20,true,false,'Flux 1.1 Pro','system'),\n"
    "          ('image','stability','stable-diffusion-3-5',30,true,false,'SD 3.5','system'),\n"
    "          ('image','google','imagen-4.0-generate-001',40,true,false,'Imagen 4.0','system'),\n"
    "          ('video','runway','gen-4-turbo',10,true,false,'Gen-4 Turbo','system'),\n"
    "          ('video','kling','kling-2.0',20,true,false,'Kling 2.0','system'),\n"
    "          ('video','pika','pika-2.2',30,true,false,'Pika 2.2','system'),\n"
    "          ('video','google','veo-3.0-generate-preview',40,true,false,'Veo 3.0','system'),\n"
    "          ('music','suno','suno-v4',10,true,true,'Suno v4','system'),\n"
    "          ('music','udio','udio-3.0',20,true,false,'Udio 3.0','system'),\n"
    "          ('music','meta','musicgen-large',30,false,false,'MusicGen Large','system'),\n"
    "          ('audio','elevenlabs','eleven-v3',10,true,true,'ElevenLabs v3','system'),\n"
    "          ('audio','openai','tts-1-hd',20,true,false,'OpenAI TTS HD','system'),\n"
    "          ('audio','google','google-wavenet-tts',30,false,false,'WaveNet TTS','system'),\n"
    "          ('runner_llm','anthropic','claude-opus-4-8',10,true,true,'Runner 기본','system'),\n"
    "          ('runner_llm','openai','gpt-5.5',20,true,false,'GPT-5.5 백업','system'),\n"
    "          ('runner_llm','google','gemini-2.5-pro',30,true,false,'Gemini 2.5 Pro 백업','system')\n"
    "        ON CONFLICT (route_key, provider, model_id) DO NOTHING\n"
    "    \"\"\")\n"
    "\n"
    "_ensure_model_routing_preferences_table() 함수 내 기존 인덱스 생성 블록 이후에 추가:\n"
    "\n"
    "        await conn.execute(\n"
    "            'ALTER TABLE model_routing_preferences '\n"
    "            'DROP CONSTRAINT IF EXISTS model_routing_preferences_route_key_chk'\n"
    "        )\n"
    "        await conn.execute(\n"
    "            \"ALTER TABLE model_routing_preferences \"\n"
    "            \"ADD CONSTRAINT model_routing_preferences_route_key_chk \"\n"
    "            \"CHECK (route_key IN \"\n"
    "            \"('image','edit_image','video','llm','music','audio','runner_llm'))\"\n"
    "        )\n"
    "        await conn.execute(\n"
    "            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS display_name TEXT'\n"
    "        )\n"
    "        await conn.execute(\n"
    "            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS family TEXT'\n"
    "        )\n"
    "        await conn.execute(\n"
    "            'ALTER TABLE model_routing_preferences ADD COLUMN IF NOT EXISTS category TEXT'\n"
    "        )\n"
    "        await _seed_media_models(conn)\n"
    "\n"
    "---\n"
    "\n"
    "#### [변경 C] get_model_routing_preferences() ORDER BY CASE 확장\n"
    "\n"
    "SQL 쿼리 내 ORDER BY CASE pref.route_key 블록에서\n"
    "WHEN 'llm' THEN 4 다음에 아래 3줄 추가:\n"
    "    WHEN 'music' THEN 5\n"
    "    WHEN 'audio' THEN 6\n"
    "    WHEN 'runner_llm' THEN 7\n"
    "\n"
    "---\n"
    "\n"
    "#### [변경 D] update_model_routing_preferences() provider 정규화 조건 수정\n"
    "\n"
    "현재: if item.route_key == 'llm'\n"
    "변경: if item.route_key in ('llm', 'runner_llm')\n"
    "\n"
    "---\n"
    "\n"
    "### 검증 (수정 후)\n"
    "docker exec aads-server bash /app/scripts/reload-api.sh\n"
    "sleep 5\n"
    "curl -s http://localhost:8080/api/v1/llm-models/routing-preferences 결과에서\n"
    "route_key 목록에 audio, music, runner_llm 포함 여부 확인\n"
    "\n"
    "### 커밋 규칙\n"
    "--no-verify 절대 금지 / docker compose up -d 전체 금지\n"
    "커밋 메시지: feat(AADS-187): media model seed + route_key expand (music/audio/runner_llm)\n"
)


def submit():
    token = create_token("system-runner", "system@aads.dev", is_admin=True)
    payload = json.dumps({
        "project": "AADS",
        "instruction": INSTRUCTION,
        "session_id": "2648cf77-4256-45e8-9cde-0e563ffefe5c",
        "max_cycles": 3,
        "size": "M",
    }).encode()
    req = urllib.request.Request(
        "http://localhost:8080/api/v1/pipeline/jobs",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        print(f"[OK] job_id={data.get('job_id')}  status={data.get('status')}")
        print(f"     msg={data.get('message')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:800]
        print(f"[HTTP {e.code}] {body}")
    except Exception as e:
        print(f"[ERR] {type(e).__name__}: {e}")


submit()
