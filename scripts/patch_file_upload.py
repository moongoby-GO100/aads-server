#!/usr/bin/env python3
"""page.tsx 파일 업로드 전환 패치: base64 인라인 → 서버 업로드"""
import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

with open(FILE, "r") as f:
    content = f.read()

# === 1. uploadChatFile 헬퍼 함수 추가 (chatApi 함수 뒤, Theme CSS 앞) ===
UPLOAD_HELPER = '''// ── File upload helper ──
async function uploadChatFile(file: File, sessionId: string): Promise<{
  file_id: string;
  original_name: string;
  mime_type: string;
  file_size: number;
  width?: number;
  height?: number;
  thumbnail_url?: string;
  file_url?: string;
}> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${BASE_URL}/chat/files/upload?session_id=${sessionId}&uploaded_by=user`, {
    method: "POST",
    headers: { ...authHdrs() },
    body: formData,
  });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

// ══════════════════════════════════════════════════════════════════
// Theme CSS variables'''

OLD_THEME = '''// ══════════════════════════════════════════════════════════════════
// Theme CSS variables'''

if "uploadChatFile" not in content:
    content = content.replace(OLD_THEME, UPLOAD_HELPER, 1)
    print("[1] uploadChatFile 헬퍼 추가 완료")
else:
    print("[1] uploadChatFile 이미 존재 — 스킵")

# === 2. handleFiles 함수 교체 ===
OLD_HANDLE = '''async function handleFiles(files: FileList | File[] | null) {
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    // 로컬 미리보기용 File 객체 즉시 저장
    setPendingPreviewFiles((prev) => [...prev, ...fileArray]);

    const IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp"]);
    const TEXT_EXTS = new Set([
      "txt", "md", "csv", "json", "py", "js", "ts", "tsx", "jsx",
      "html", "css", "yaml", "yml", "toml", "sh", "sql", "log",
      "xml", "ini", "conf", "cfg", "rs", "go", "java", "c", "cpp",
      "h", "rb", "php", "swift", "kt",
    ]);
    const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "avi", "mkv", "flv", "m4v"]);
    const VIDEO_MAX_BYTES = 20 * 1024 * 1024; // 20MB

    for (const file of fileArray) {
      const ext = file.name.split(".").pop()?.toLowerCase() || "";
      const isImage = IMAGE_EXTS.has(ext) || file.type.startsWith("image/");
      const isText = TEXT_EXTS.has(ext) || file.type.startsWith("text/");
      const isVideo = VIDEO_EXTS.has(ext) || file.type.startsWith("video/");

      if (isImage) {
        // 이미지: base64 인코딩 → Claude Vision API로 전달
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => {
            const result = reader.result as string;
            resolve(result.split(",")[1] ?? ""); // "data:image/...;base64,XXX" → "XXX"
          };
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        const mediaType = file.type || `image/${ext === "jpg" ? "jpeg" : ext}`;
        pendingAttachments.current.push({ type: "image", base64, media_type: mediaType, name: file.name });
      } else if (isText) {
        // 텍스트 파일: 최대 500KB 내용 읽기 → ephemeral document layer로 전달
        const content = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = () => resolve("");
          reader.readAsText(file.slice(0, 500_000));
        });
        pendingAttachments.current.push({ type: "text", name: file.name, content });
      } else if (ext === "pdf" || file.type === "application/pdf") {
        // PDF: base64 인코딩 → 서버에서 텍스트 추출
        const base64 = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => {
            const result = reader.result as string;
            resolve(result.split(",")[1] ?? "");
          };
          reader.onerror = () => resolve("");
          reader.readAsDataURL(file);
        });
        pendingAttachments.current.push({ type: "pdf", base64, name: file.name, media_type: "application/pdf" });
      } else if (isVideo) {
        // 동영상: 20MB 이하 → base64 인코딩 → Gemini API 분석
        if (file.size > VIDEO_MAX_BYTES) {
          pendingAttachments.current.push({ type: "file", name: file.name, error: `동영상 파일이 너무 큽니다 (최대 20MB). 현재: ${(file.size / 1024 / 1024).toFixed(1)}MB` });
        } else {
          const base64 = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => {
              const result = reader.result as string;
              resolve(result.split(",")[1] ?? "");
            };
            reader.onerror = () => resolve("");
            reader.readAsDataURL(file);
          });
          const mediaType = file.type || `video/${ext}`;
          pendingAttachments.current.push({ type: "video", base64, name: file.name, media_type: mediaType });
        }
      } else {
        // 기타 파일: 이름만 기록
        pendingAttachments.current.push({ type: "file", name: file.name });
      }
    }
    textareaRef.current?.focus();
  }'''

NEW_HANDLE = '''async function handleFiles(files: FileList | File[] | null) {
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    // 로컬 미리보기용 File 객체 즉시 저장
    setPendingPreviewFiles((prev) => [...prev, ...fileArray]);

    const IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp"]);
    const TEXT_EXTS = new Set([
      "txt", "md", "csv", "json", "py", "js", "ts", "tsx", "jsx",
      "html", "css", "yaml", "yml", "toml", "sh", "sql", "log",
      "xml", "ini", "conf", "cfg", "rs", "go", "java", "c", "cpp",
      "h", "rb", "php", "swift", "kt",
    ]);
    const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "avi", "mkv", "flv", "m4v"]);
    const VIDEO_MAX_BYTES = 20 * 1024 * 1024; // 20MB
    const _sid = activeSession?.id;

    for (const file of fileArray) {
      const ext = file.name.split(".").pop()?.toLowerCase() || "";
      const isImage = IMAGE_EXTS.has(ext) || file.type.startsWith("image/");
      const isText = TEXT_EXTS.has(ext) || file.type.startsWith("text/");
      const isVideo = VIDEO_EXTS.has(ext) || file.type.startsWith("video/");

      // 이미지: 서버 업로드 → file_id 기반 (fallback: base64)
      if (isImage && _sid) {
        try {
          const result = await uploadChatFile(file, _sid);
          pendingAttachments.current.push({
            type: "image", file_id: result.file_id,
            media_type: result.mime_type, name: result.original_name,
            file_url: result.file_url, thumbnail_url: result.thumbnail_url,
          });
        } catch {
          // fallback: base64
          const base64 = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = reject;
            reader.readAsDataURL(file);
          });
          const mediaType = file.type || `image/${ext === "jpg" ? "jpeg" : ext}`;
          pendingAttachments.current.push({ type: "image", base64, media_type: mediaType, name: file.name });
        }
      } else if (isImage) {
        // 세션 없으면 기존 base64 방식
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        const mediaType = file.type || `image/${ext === "jpg" ? "jpeg" : ext}`;
        pendingAttachments.current.push({ type: "image", base64, media_type: mediaType, name: file.name });
      } else if (isText) {
        // 텍스트 파일: 최대 500KB 내용 읽기 → ephemeral document layer로 전달
        const content = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = () => resolve("");
          reader.readAsText(file.slice(0, 500_000));
        });
        pendingAttachments.current.push({ type: "text", name: file.name, content });
      } else if (ext === "pdf" || file.type === "application/pdf") {
        // PDF: 서버 업로드 시도 → fallback base64
        if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "pdf", file_id: result.file_id, name: result.original_name,
              media_type: "application/pdf", file_url: result.file_url,
            });
          } catch {
            const base64 = await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
              reader.onerror = () => resolve("");
              reader.readAsDataURL(file);
            });
            pendingAttachments.current.push({ type: "pdf", base64, name: file.name, media_type: "application/pdf" });
          }
        } else {
          const base64 = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = () => resolve("");
            reader.readAsDataURL(file);
          });
          pendingAttachments.current.push({ type: "pdf", base64, name: file.name, media_type: "application/pdf" });
        }
      } else if (isVideo) {
        // 동영상: 20MB 이하 → 서버 업로드 시도 → fallback base64
        if (file.size > VIDEO_MAX_BYTES) {
          pendingAttachments.current.push({ type: "file", name: file.name, error: `동영상 파일이 너무 큽니다 (최대 20MB). 현재: ${(file.size / 1024 / 1024).toFixed(1)}MB` });
        } else if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "video", file_id: result.file_id, name: result.original_name,
              media_type: result.mime_type, file_url: result.file_url,
            });
          } catch {
            const base64 = await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
              reader.onerror = () => resolve("");
              reader.readAsDataURL(file);
            });
            const mediaType = file.type || `video/${ext}`;
            pendingAttachments.current.push({ type: "video", base64, name: file.name, media_type: mediaType });
          }
        } else {
          const base64 = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = () => resolve("");
            reader.readAsDataURL(file);
          });
          const mediaType = file.type || `video/${ext}`;
          pendingAttachments.current.push({ type: "video", base64, name: file.name, media_type: mediaType });
        }
      } else {
        // 기타 파일: 서버 업로드 시도
        if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "file", file_id: result.file_id, name: result.original_name,
              file_url: result.file_url, file_size: result.file_size,
            });
          } catch {
            pendingAttachments.current.push({ type: "file", name: file.name });
          }
        } else {
          pendingAttachments.current.push({ type: "file", name: file.name });
        }
      }
    }
    textareaRef.current?.focus();
  }'''

if OLD_HANDLE in content:
    content = content.replace(OLD_HANDLE, NEW_HANDLE, 1)
    print("[2] handleFiles 함수 교체 완료")
else:
    print("[2] ERROR: handleFiles 원본 매칭 실패!")
    # 디버깅: 시작 부분 확인
    idx = content.find("async function handleFiles")
    if idx >= 0:
        print(f"    handleFiles 발견 위치: {idx}")
        print(f"    주변 내용: {content[idx:idx+100]}")

# === 3. activeSid 변수명 확인 ===
if "activeSid" not in content and "activeSession?.id" in content:
    print("[INFO] activeSid 없음 — activeSession?.id 사용")

with open(FILE, "w") as f:
    f.write(content)

print("[완료] page.tsx 패치 적용됨")
