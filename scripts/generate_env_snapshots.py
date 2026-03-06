"""Context API에서 서버 환경 스냅샷 읽어서 정적 JSON 생성"""
import requests, json, os
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
API = "http://localhost:8100/api/v1"
OUTPUT_DIR = "/root/aads/aads-dashboard/public/manager"

os.makedirs(OUTPUT_DIR, exist_ok=True)

for server in ["68", "211", "114"]:
    try:
        r = requests.get(f"{API}/context/system", params={"category": "server_environment", "key": f"env_{server}"}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            with open(f"{OUTPUT_DIR}/env_{server}.json", "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

# 통합 인덱스
index = {
    "generated_at": datetime.now(KST).isoformat(),
    "servers": ["68", "211", "114"],
    "urls": {
        "68": "/manager/env_68.json",
        "211": "/manager/env_211.json",
        "114": "/manager/env_114.json",
    }
}
with open(f"{OUTPUT_DIR}/env_index.json", "w") as f:
    json.dump(index, f, ensure_ascii=False, indent=2)

print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] env snapshots generated")
