-- transfer_bytes 는 실제로 "압축 해제 후 크기"를 담고 있었다.
--
-- Resource Timing 의 transferSize 는 zstd/brotli 로 나가면 0 으로 오고
-- encodedBodySize 도 decodedBodySize 와 같아진다. 세 값을 합쳐 쓰는 바람에
-- 압축 전 크기가 "전송량"으로 기록됐다 — /dashboard/directives 를 1.55MB
-- 전송이라고 적었지만 실제 전송은 126KB 였다(12배 차이).
--
-- 이름을 사실에 맞춘다. payload_bytes 는 대역폭이 아니라 브라우저의 파싱·메모리
-- 비용을 보는 지표이고, wire_bytes 는 실제 전송량이되 측정되지 않을 수 있다.
ALTER TABLE frontend_diagnostic_pages RENAME COLUMN transfer_bytes TO payload_bytes;
ALTER TABLE frontend_diagnostic_pages ADD COLUMN IF NOT EXISTS wire_bytes bigint;
