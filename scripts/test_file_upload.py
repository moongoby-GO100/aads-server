#!/usr/bin/env python3
"""파일 업로드 API 테스트 (컨테이너 내부, port 8080)"""
import json, os

# 1. 테스트 이미지 생성
try:
    from PIL import Image
    img = Image.new('RGB', (200, 200), color='blue')
    img.save('/tmp/test_upload.png')
    print("[1] Test image created: /tmp/test_upload.png (200x200 blue)")
except Exception as e:
    print("[1] PIL error, using fallback")
    import struct, zlib
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data)
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc & 0xffffffff)
    raw = b'\x00\xff\x00\x00'
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b'IDAT' + compressed)
    idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc & 0xffffffff)
    iend_crc = zlib.crc32(b'IEND')
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc & 0xffffffff)
    with open('/tmp/test_upload.png', 'wb') as f:
        f.write(sig + ihdr + idat + iend)
    print("[1] Test PNG created (1x1 fallback)")

# 2. 세션 ID
SESSION_ID = "06d7ec65-4f46-4a04-a0ea-c8ac4ccef56c"

# 3. multipart upload
import urllib.request
import urllib.error

url = "http://127.0.0.1:8080/api/v1/chat/files/upload?session_id=%s&uploaded_by=user" % SESSION_ID

boundary = "----TestBoundary12345"
file_data = open('/tmp/test_upload.png', 'rb').read()
print("[2] File size: %d bytes" % len(file_data))

body = (
    ("--%s\r\n" % boundary).encode() +
    b'Content-Disposition: form-data; name="file"; filename="test_upload.png"\r\n' +
    b'Content-Type: image/png\r\n\r\n' +
    file_data +
    ("\r\n--%s--\r\n" % boundary).encode()
)

req = urllib.request.Request(url, data=body, method='POST')
req.add_header('Content-Type', 'multipart/form-data; boundary=%s' % boundary)
req.add_header('X-Monitor-Key', 'internal-test')

try:
    resp = urllib.request.urlopen(req, timeout=15)
    result = json.loads(resp.read().decode())
    print("[3] Upload SUCCESS!")
    for k, v in result.items():
        print("    %s: %s" % (k, v))
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print("[3] HTTP Error %d: %s" % (e.code, body[:500]))
except Exception as e:
    print("[3] Error: %s" % str(e))

# 4. 파일 조회 테스트
if 'result' in dir() and isinstance(result, dict) and result.get('file_id'):
    fid = result['file_id']
    get_url = "http://127.0.0.1:8080/api/v1/chat/files/%s" % fid
    req2 = urllib.request.Request(get_url)
    req2.add_header('X-Monitor-Key', 'internal-test')
    try:
        resp2 = urllib.request.urlopen(req2, timeout=10)
        print("[4] File GET: status=%d, content-type=%s, size=%s" % (
            resp2.status,
            resp2.headers.get('content-type', '?'),
            resp2.headers.get('content-length', '?'),
        ))
    except Exception as e:
        print("[4] File GET error: %s" % str(e))

    # 5. 썸네일 테스트
    thumb_url = "http://127.0.0.1:8080/api/v1/chat/files/%s/thumbnail" % fid
    req3 = urllib.request.Request(thumb_url)
    req3.add_header('X-Monitor-Key', 'internal-test')
    try:
        resp3 = urllib.request.urlopen(req3, timeout=10)
        print("[5] Thumbnail GET: status=%d, content-type=%s, size=%s" % (
            resp3.status,
            resp3.headers.get('content-type', '?'),
            resp3.headers.get('content-length', '?'),
        ))
    except Exception as e:
        print("[5] Thumbnail GET error: %s" % str(e))
