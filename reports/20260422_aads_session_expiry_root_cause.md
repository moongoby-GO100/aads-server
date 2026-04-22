# AADS Session Expiry Root Cause

## 결론

- 원인: 대시보드 공용 인증 유틸은 `7일` 쿠키로 바뀌었지만, `signup` 페이지에 남아 있는 수동 `24시간` 쿠키 덮어쓰기 때문에 회원가입 사용자 세션이 하루 후 끊깁니다.
- 증상 경로: 쿠키 만료 후 대시보드 미들웨어가 인증 쿠키 부재를 감지해 로그인으로 보냅니다.

## 근거

1. 백엔드 JWT 만료는 이미 7일입니다.

```py
app/auth.py
TOKEN_EXPIRE_HOURS = 24 * 7
```

2. 프론트 공용 인증 유틸도 이미 7일로 수정되어 있습니다.

```ts
/root/aads/aads-dashboard/src/lib/auth.ts
const COOKIE_MAX_AGE = 24 * 7 * 3600;
```

3. 그런데 `signup` 페이지가 같은 쿠키를 다시 24시간으로 덮어씁니다.

```ts
/root/aads/aads-dashboard/src/app/signup/page.tsx
const token = await register(email, password, name);
document.cookie = `aads_token=${token}; path=/; max-age=${24 * 3600}; SameSite=Lax`;
```

4. 대시보드 미들웨어는 `aads_token` 쿠키만 보고 인증 여부를 판단합니다.

```ts
/root/aads/aads-dashboard/src/middleware.ts
const token = request.cookies.get("aads_token")?.value;
if (!token) redirect("/login")
```

## 필요한 수정

`signup/page.tsx`에서 수동 쿠키 덮어쓰기 줄을 제거하고 `register()`가 처리한 공용 `auth.ts` 경로만 사용해야 합니다.

```diff
diff --git a/src/app/signup/page.tsx b/src/app/signup/page.tsx
@@
-      const token = await register(email, password, name);
-      document.cookie = `aads_token=${token}; path=/; max-age=${24 * 3600}; SameSite=Lax`;
+      await register(email, password, name);
       router.push("/kakaobot");
```

## 작업 제약

- 현재 Codex 샌드박스에서는 `/root/aads/aads-dashboard`가 읽기 전용으로 마운트되어 직접 적용은 실패했습니다.
- 실패 메시지: `Read-only file system`
