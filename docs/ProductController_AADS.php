<?php
/**
 * T-030: 뉴톡 V2 Laravel — AADS 이미지 품질 게이트 연동
 * 116서버 app/Http/Controllers/ProductController.php 에 통합
 *
 * 적용 조건: CEO 승인 후 (완료 기준 §5)
 * AADS Client: /root/aads_qa/aads_qa_client.sh
 * AADS API:    https://aads.newtalk.kr/api/v1/visual-qa
 */

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Http;

class ProductController extends Controller
{
    /**
     * 방법 A: Shell 클라이언트 직접 호출 (권장)
     * /root/aads_qa/aads_qa_client.sh image-gate <path> newtalk_v2 <id>
     * 반환: 0=APPROVE, 1=REJECT
     */
    private function qualityGate(string $imagePath, string $productId = ''): bool
    {
        $clientPath = '/root/aads_qa/aads_qa_client.sh';
        $project    = 'newtalk_v2';
        $imageId    = $productId ?: 'product_' . time();

        // 경로 내 특수문자 방지 (보안)
        $safePath  = escapeshellarg($imagePath);
        $safeId    = escapeshellarg($imageId);

        $output   = [];
        $exitCode = null;
        exec("{$clientPath} image-gate {$safePath} {$project} {$safeId} 2>&1", $output, $exitCode);

        Log::info('[AADS] image-gate', [
            'image_id'  => $imageId,
            'exit_code' => $exitCode,
            'output'    => implode("\n", $output),
        ]);

        return $exitCode === 0;
    }

    /**
     * store() 메서드: 이미지 업로드 + 품질 게이트
     */
    public function store(Request $request)
    {
        $request->validate([
            'image'      => 'required|image|max:10240',
            'product_id' => 'required|string',
        ]);

        $productId = $request->input('product_id');

        // 1. 임시 저장
        $imagePath    = $request->file('image')->store('products/temp');
        $absolutePath = storage_path("app/{$imagePath}");

        // 2. AADS 이미지 품질 게이트
        if (!$this->qualityGate($absolutePath, $productId)) {
            Storage::delete($imagePath);
            return response()->json([
                'error' => '이미지 품질 기준 미달 (AADS QA REJECT)',
                'guide' => '해상도 800x800+ / 깨끗한 배경 / 상품 60%+ 노출 / 색감 자연스러움 필요',
            ], 422);
        }

        // 3. 품질 통과 → 정식 저장 경로로 이동
        $finalPath = "products/{$productId}/" . basename($imagePath);
        Storage::move($imagePath, $finalPath);

        return response()->json(['path' => $finalPath], 201);
    }

    /**
     * 방법 B: PHP HTTP 클라이언트 직접 호출 (Shell 없이 사용 시)
     * POST https://aads.newtalk.kr/api/v1/visual-qa/image-quality-gate
     */
    private function qualityGateHttp(string $absolutePath, string $productId): bool
    {
        $imageBase64 = base64_encode(file_get_contents($absolutePath));

        $response = Http::timeout(60)->post(
            'https://aads.newtalk.kr/api/v1/visual-qa/image-quality-gate',
            [
                'project_id'   => 'newtalk_v2',
                'image_base64' => $imageBase64,
                'image_id'     => $productId,
                'min_score'    => 48,
            ]
        );

        $result = $response->json();

        Log::info('[AADS] image-quality-gate HTTP', [
            'image_id'    => $productId,
            'action'      => $result['action'] ?? 'unknown',
            'total_score' => $result['total_score'] ?? 0,
        ]);

        return ($result['action'] ?? '') === 'approve';
    }

    /**
     * 배치 이미지 검수 (상품 등록 시 여러 장)
     * POST https://aads.newtalk.kr/api/v1/visual-qa/image-qa
     */
    public function storeMultiple(Request $request)
    {
        $request->validate([
            'images'     => 'required|array|min:1|max:10',
            'images.*'   => 'required|image|max:10240',
            'product_id' => 'required|string',
        ]);

        $productId = $request->input('product_id');
        $images    = [];

        foreach ($request->file('images') as $idx => $file) {
            $images[] = [
                'image_base64' => base64_encode($file->get()),
                'image_id'     => "product_{$productId}_img{$idx}",
                'category'     => '상품',
            ];
        }

        $response = Http::timeout(120)->post(
            'https://aads.newtalk.kr/api/v1/visual-qa/image-qa',
            [
                'project_id' => 'newtalk_v2',
                'images'     => $images,
            ]
        );

        $qa      = $response->json();
        $rejects = [];

        foreach ($qa['results'] ?? [] as $result) {
            if (($result['verdict'] ?? '') === 'FAIL') {
                $rejects[] = $result['image_id'];
                Log::warning('[AADS] image-qa FAIL', [
                    'image_id' => $result['image_id'],
                    'score'    => $result['total_score'] ?? 0,
                    'summary'  => $result['summary'] ?? '',
                ]);
            }
        }

        if (!empty($rejects)) {
            return response()->json([
                'error'         => '일부 이미지 품질 미달',
                'rejected_ids'  => $rejects,
                'qa_scorecard'  => $qa,
            ], 422);
        }

        return response()->json(['qa_scorecard' => $qa, 'status' => 'all_passed'], 200);
    }
}
