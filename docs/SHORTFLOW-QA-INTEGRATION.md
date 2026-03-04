# ShortFlow 품질 게이트 연동 가이드

> AADS T-027+T-028+T-029 | 211서버(ShortFlow/GO100) → 68서버(AADS) 연동

---

## 1. 클라이언트 배포 (68서버에서 실행)

```bash
# 211서버 IP 설정
export SF211_IP=<211서버_실제_IP>

# 원클릭 배포 스크립트 실행 (T-029)
bash /root/aads/aads-server/scripts/deploy_to_211.sh $SF211_IP

# 수동 배포 (스크립트 사용 불가 시)
scp /root/aads/aads-server/scripts/aads_qa_client.sh root@$SF211_IP:/root/aads_qa_client.sh
scp /root/aads/aads-server/scripts/run_v4_pipeline_qa_patch.py root@$SF211_IP:/root/aads_qa/run_v4_pipeline_qa_patch.py
ssh root@$SF211_IP "chmod +x /root/aads_qa_client.sh && echo 'export AADS_QA_URL=https://aads.newtalk.kr/api/v1/visual-qa' >> /root/.bashrc"

# 확인
ssh root@$SF211_IP "/root/aads_qa_client.sh --help"
```

---

## 2. 벤치마크 등록 (최초 1회, 채널별)

```bash
ssh root@$SF211_IP << 'REMOTE'
source /root/.bashrc

# economy 채널 벤치마크 등록
/root/aads_qa_client.sh quality-gate /data/shortflow/outputs/economy/best_video.mp4 shortflow economy eco_benchmark

# health 채널 벤치마크 등록
/root/aads_qa_client.sh quality-gate /data/shortflow/outputs/health/best_video.mp4 shortflow health health_benchmark
REMOTE
```

---

## 3. run_v4_pipeline.py 수정 (T-029)

`/data/shortflow/run_v4_pipeline.py` 업로드 직전 위치에 아래 코드를 삽입한다.

### 방법 A: 모듈 import (권장)

```bash
# 211서버에서 실행 — 패치 모듈을 ShortFlow 디렉토리에 복사
cp /root/aads_qa/run_v4_pipeline_qa_patch.py /data/shortflow/run_v4_pipeline_qa_patch.py
```

```python
# run_v4_pipeline.py 상단 import 섹션에 추가:
import sys
sys.path.insert(0, "/root/aads_qa")   # 또는 /data/shortflow/
from run_v4_pipeline_qa_patch import quality_gate_before_upload

# ── upload 직전 함수 추가 ────────────────────────────────────────────────────

def run_quality_gate(output_path: str, channel_name: str) -> bool:
    """
    AADS 품질 게이트 실행.
    Returns: True = 업로드 진행, False = 중단
    """
    from datetime import datetime
    video_id = f"{channel_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    verdict = quality_gate_before_upload(
        video_path=output_path,
        channel=channel_name,
        video_id=video_id,
    )

    if verdict == "publish":
        logger.info(f"[QA] AUTO_PUBLISH: {video_id}")
        return True
    elif verdict == "hold":
        logger.warning(f"[QA] CONDITIONAL: {video_id} → CEO 리뷰 큐 저장")
        import shutil, os
        review_dir = "/data/shortflow/review_queue"
        os.makedirs(review_dir, exist_ok=True)
        shutil.copy2(output_path, f"{review_dir}/{os.path.basename(output_path)}")
        return False
    elif verdict == "reject":
        logger.error(f"[QA] AUTO_REJECT: {video_id} → 재렌더링 대기")
        return False
    else:  # error
        logger.warning(f"[QA] ERROR (fail-open): {video_id} → 업로드 진행")
        return True   # fail-open: AADS 오류 시 기존 흐름 유지

# 기존 업로드 코드 수정:
# 기존:
#   upload_to_youtube(output_path, channel_name, ...)
#
# 변경 후:
#   if run_quality_gate(output_path, channel_name):
#       upload_to_youtube(output_path, channel_name, ...)
#   else:
#       logger.info(f"업로드 건너뜀 (품질 게이트 미통과): {output_path}")
```

### 방법 B: subprocess 직접 호출 (모듈 import 불가 시)

```python
import subprocess

def quality_check(video_path, channel, video_id):
    """AADS 품질 게이트 — 업로드 직전 호출"""
    result = subprocess.run(
        ['/root/aads_qa_client.sh', 'quality-gate', video_path, 'shortflow', channel, video_id],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        print(f"✅ AUTO_PUBLISH: {video_id}")
        return "publish"
    elif result.returncode == 2:
        print(f"⚠️ CONDITIONAL: {video_id} — CEO 리뷰 대기")
        return "hold"
    elif result.returncode == 3:
        print(f"❌ REJECT: {video_id}")
        return "reject"
    else:
        print(f"⚠️ QA ERROR (fail-open): {video_id}")
        return "publish"  # fail-open

# 기존 upload 함수 내에서:
# verdict = quality_check(output_path, channel_name, video_id)
# if verdict == "publish":
#     upload_to_youtube(...)
# elif verdict == "hold":
#     save_for_review(...)  # CEO 확인 후 수동 업로드
# else:
#     log_rejection(...)
```

---

## 4. cron에서 자동 검수 (선택)

```bash
# /etc/crontab에 추가 — 매일 23:00 당일 생성 영상 일괄 검수
0 23 * * * root /root/aads_qa_client.sh quality-gate /data/shortflow/outputs/economy/latest.mp4 shortflow economy eco_$(date +\%Y\%m\%d) 2>> /var/log/aads_qa.log
```

---

## 5. 반환 코드

| 코드 | 의미 | 처리 |
|------|------|------|
| 0 | AUTO_PUBLISH (85%+) | 즉시 업로드 |
| 2 | CONDITIONAL (70-84%) | CEO 리뷰 큐 저장 |
| 3 | AUTO_REJECT (<70%) | 재렌더링 지시서 확인 |
| 1 | ERROR | fail-open (업로드 진행) |

---

## 6. 배포 파일 목록 (T-027+T-028+T-029)

| 파일 (211서버) | 출처 | 설명 |
|---------------|------|------|
| `/root/aads_qa_client.sh` | scripts/aads_qa_client.sh | AADS QA 클라이언트 (quality-gate/image-qa/image-gate) |
| `/root/aads_qa/run_v4_pipeline_qa_patch.py` | scripts/run_v4_pipeline_qa_patch.py | run_v4_pipeline.py 연동 모듈 |
| `/root/aads_qa/setup.sh` | scripts/aads_qa_local/setup.sh | 의존성 설치 |
| `/root/aads_qa/quality_gate.sh` | scripts/aads_qa_local/quality_gate.sh | 로컬 품질 게이트 (독립 실행) |
| `/root/aads_qa/auditor.py` | scripts/aads_qa_local/auditor.py | 로컬 감리기 |

---

## 7. 서버 구성 현황

| 서버 | 역할 | 클라이언트 |
|------|------|-----------|
| 68서버 (AADS) | 중앙 검수 엔진 | — |
| 211서버 (ShortFlow) | 영상 생성·업로드 | /root/aads_qa_client.sh |
| 116서버 (뉴톡 V2) | 이미지 저장·서빙 | /root/aads_qa_client.sh |

---

## 8. API 엔드포인트

- `POST https://aads.newtalk.kr/api/v1/visual-qa/quality-gate` — 영상 품질 게이트
- `GET  https://aads.newtalk.kr/api/v1/visual-qa/benchmark-specs/{project_id}/{channel_name}` — 벤치마크 조회
- `POST https://aads.newtalk.kr/api/v1/visual-qa/extract-spec` — 벤치마크 등록

---

*생성: AADS T-028 | 업데이트: T-029 | 2026-03-04*
