# 뉴톡 V2 이미지 품질 게이트 연동 가이드

> AADS T-028 | 116서버(뉴톡 V2 Laravel) → 68서버(AADS) 연동

---

## 1. 클라이언트 설치 (68서버에서 실행)

```bash
# 116서버에 클라이언트 배포
scp /root/aads/aads-server/scripts/aads_qa_client.sh root@[116-IP]:/root/aads_qa_client.sh
ssh root@[116-IP] "chmod +x /root/aads_qa_client.sh && echo 'export AADS_QA_URL=https://aads.newtalk.kr/api/v1/visual-qa' >> /root/.bashrc"

# 확인
ssh root@[116-IP] "/root/aads_qa_client.sh --help"
```

---

## 2. Laravel 컨트롤러 연동

### 방법 A: Shell 클라이언트 호출 (권장)

```php
// ProductImageController.php — 이미지 저장 직전

public function store(Request $request)
{
    $imagePath = $request->file('image')->store('products/temp');
    $absolutePath = storage_path("app/{$imagePath}");
    $productId = $request->input('product_id');

    // AADS 이미지 품질 게이트
    $qaResult = shell_exec("/root/aads_qa_client.sh image-gate {$absolutePath} newtalk_v2 {$productId} 2>&1");
    $exitCode = null;
    exec("/root/aads_qa_client.sh image-gate {$absolutePath} newtalk_v2 {$productId}", $output, $exitCode);

    if ($exitCode !== 0) {
        // 품질 미달 — 저장 거부
        Storage::delete($imagePath);
        return response()->json([
            'error' => '이미지 품질 기준 미달',
            'qa_output' => implode("\n", $output),
        ], 422);
    }

    // 품질 통과 — 정식 저장 경로로 이동
    $finalPath = "products/{$productId}/" . basename($imagePath);
    Storage::move($imagePath, $finalPath);

    return response()->json(['path' => $finalPath], 201);
}
```

### 방법 B: PHP HTTP 클라이언트 직접 호출

```php
use Illuminate\Support\Facades\Http;

$imageBase64 = base64_encode(file_get_contents($absolutePath));

$response = Http::timeout(60)->post('https://aads.newtalk.kr/api/v1/visual-qa/image-quality-gate', [
    'project_id'    => 'newtalk_v2',
    'image_base64'  => $imageBase64,
    'image_id'      => $productId,
    'min_score'     => 48,
]);

$result = $response->json();

if ($result['action'] === 'approve') {
    // 저장 진행
} else {
    // 저장 거부
    return response()->json([
        'error'  => '이미지 품질 미달',
        'issues' => $result['issues'] ?? [],
        'score'  => $result['total_score'] ?? 0,
    ], 422);
}
```

---

## 3. 이미지 일괄 검수 (상품 등록 시 여러 장)

```php
// POST /api/v1/visual-qa/image-qa 사용
$images = [];
foreach ($request->file('images') as $idx => $file) {
    $images[] = [
        'image_base64' => base64_encode($file->get()),
        'image_id'     => "product_{$productId}_img{$idx}",
        'category'     => '상품',
    ];
}

$response = Http::timeout(120)->post('https://aads.newtalk.kr/api/v1/visual-qa/image-qa', [
    'project_id' => 'newtalk_v2',
    'images'     => $images,
]);

$qa = $response->json();

foreach ($qa['results'] as $result) {
    if ($result['verdict'] === 'FAIL') {
        // 해당 이미지 거부
        Log::warning("이미지 품질 미달: {$result['image_id']} — {$result['summary']}");
    }
}
```

---

## 4. 검수 기준 (이커머스 6항목)

| 항목 | 기준 | 배점 |
|------|------|------|
| resolution_clarity | 최소 800x800, 블러없음, 노이즈없음 | 10점 |
| background_quality | 깨끗한 배경, 불필요 요소 없음, 일관성 | 10점 |
| product_visibility | 상품이 화면의 60%+, 잘림없음, 그림자적절 | 10점 |
| color_accuracy | 자연스러운 색감, 과보정없음, 화이트밸런스 | 10점 |
| text_overlay | 가독성, 위치적절, 상품가림없음 (없으면 10점) | 10점 |
| commercial_readiness | 구매 전환 유도, 신뢰감, 프로 수준 | 10점 |

**판정**: PASS 48+(80%) / CONDITIONAL 36-47(60-79%) / FAIL 35이하

---

## 5. API 엔드포인트

- `POST https://aads.newtalk.kr/api/v1/visual-qa/image-qa` — 이미지 일괄 검수
- `POST https://aads.newtalk.kr/api/v1/visual-qa/image-quality-gate` — 단일 이미지 품질 게이트

---

## 6. 서버 구성 현황

| 서버 | 역할 | 클라이언트 |
|------|------|-----------|
| 68서버 (AADS) | 중앙 검수 엔진 | — |
| 211서버 (ShortFlow) | 영상 생성·업로드 | /root/aads_qa_client.sh |
| 116서버 (뉴톡 V2) | 이미지 저장·서빙 | /root/aads_qa_client.sh |

---

*생성: AADS T-028 | 2026-03-04*
