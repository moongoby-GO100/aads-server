-- 20261028_skill_guide_ko.sql 되돌림. 공용 스킬 metadata 에서 guide 키만 제거한다(다른 키·행은 그대로).
UPDATE ops_skill_library
SET metadata = metadata - 'guide'
WHERE tenant_id = '00000000-0000-0000-0000-000000000000'::uuid
  AND slug IN (
      'aads-bluegreen-release', 'runner-recovery', 'authenticated-site-collector',
      'store-assistant-channel-collector', 'go100-market-open-check', 'kis-broker-health',
      'ntv2-merchant-contract', 'sf-video-pipeline-health', 'nas-image-job-health'
  );
