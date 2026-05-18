#!/bin/bash
# AI 모델 이미지 갤러리 자동 동기화 (1분마다 cron)
docker exec aads-server python3 /app/scripts/export_gallery.py >> /var/log/gallery_sync.log 2>&1
mkdir -p /var/www/aads-public/reports/gallery
cp -f /root/aads/aads-server/app/static/gallery/* /var/www/aads-public/reports/gallery/ >> /var/log/gallery_sync.log 2>&1
chmod -R a+rX /var/www/aads-public/reports/gallery >> /var/log/gallery_sync.log 2>&1
chcon -R -t httpd_sys_content_t /root/aads/aads-server/app/static/gallery/ 2>/dev/null
