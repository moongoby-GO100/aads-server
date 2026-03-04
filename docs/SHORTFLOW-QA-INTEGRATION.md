# ShortFlow 품질 게이트 연동 가이드

> AADS T-027+T-028 | 211서버(ShortFlow/GO100) → 68서버(AADS) 연동

---

## 1. 클라이언트 설치 (68서버에서 실행)

```bash
# 211서버에 클라이언트 배포
scp /root/aads/aads-server/scripts/aads_qa_client.sh root@[211-IP]:/root/aads_qa_client.sh
ssh root@[211-IP] "chmod +x /root/aads_qa_client.sh && echo 'export AADS_QA_URL=https://aads.newtalk.kr/api/v1/visual-qa' >> /root/.bashrc"

# 확인
ssh root@[211-IP] "/root/aads_qa_client.sh --help"
```

---

## 2. 벤치마크 등록 (최초 1회, 채널별)

```bash
ssh root@[211-IP] << 'REMOTE'
source /root/.bashrc

/root/aads_qa_client.sh quality-gate /data/shortflow/outputs/economy/best_video.mp4 shortflow economy eco_benchmark
/root/aads_qa_client.sh quality-gate /data/shortflow/outputs/health/best_video.mp4 shortflow health health_benchmark
REMOTE
```

---

## 3. run_v4_pipeline.py 수정 (업로드 직전에 삽입)

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
    else:
        print(f"❌ REJECT: {video_id}")
        return "reject"

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
| 1 | ERROR | 로그 확인, 재시도 |

---

## 6. 서버 구성 현황

| 서버 | 역할 | 클라이언트 |
|------|------|-----------|
| 68서버 (AADS) | 중앙 검수 엔진 | — |
| 211서버 (ShortFlow) | 영상 생성·업로드 | /root/aads_qa_client.sh |
| 116서버 (뉴톡 V2) | 이미지 저장·서빙 | /root/aads_qa_client.sh |

---

## 7. API 엔드포인트

- `POST https://aads.newtalk.kr/api/v1/visual-qa/quality-gate` — 영상 품질 게이트
- `GET  https://aads.newtalk.kr/api/v1/visual-qa/benchmark-specs/{project_id}/{channel_name}` — 벤치마크 조회
- `POST https://aads.newtalk.kr/api/v1/visual-qa/extract-spec` — 벤치마크 등록

---

*생성: AADS T-028 | 2026-03-04*
