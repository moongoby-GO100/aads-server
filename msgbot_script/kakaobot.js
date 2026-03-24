/**
 * AADS 메신저봇R 스크립트 — 카카오톡 AI 자동 응답
 *
 * 사용법:
 *   1. 메신저봇R 앱 설치
 *   2. 이 스크립트를 "봇 추가" → "JavaScript" 로 붙여넣기
 *   3. SERVER_URL, BOT_TOKEN 설정 후 컴파일 & 활성화
 */

// ── 설정 (사용자가 수정) ────────────────────────────────────────────
var SERVER_URL = "https://aads.newtalk.kr/api/v1/kakao-bot/msgbot/webhook";
var BOT_TOKEN = "여기에_토큰_입력";

// 그룹채팅에서 봇을 호출하는 키워드 (예: "@봇", "!봇")
var BOT_TRIGGER = "@봇";

// 타임아웃 (밀리초)
var TIMEOUT_MS = 10000;

// ── 메신저봇R 메인 응답 함수 ────────────────────────────────────────
function response(room, msg, sender, isGroupChat, replier, imageDB, packageName) {
    // 그룹채팅에서는 BOT_TRIGGER가 포함된 경우에만 응답
    if (isGroupChat && msg.indexOf(BOT_TRIGGER) === -1) {
        return;
    }

    // 트리거 키워드 제거
    var cleanMsg = msg.replace(BOT_TRIGGER, "").trim();
    if (!cleanMsg) return;

    try {
        var data = {
            "room": room,
            "sender": sender,
            "message": cleanMsg,
            "isGroupChat": isGroupChat,
            "bot_token": BOT_TOKEN
        };

        var jsonBody = JSON.stringify(data);

        // org.jsoup (메신저봇R 내장) 으로 HTTP POST
        var Jsoup = org.jsoup.Jsoup;
        var resp = Jsoup.connect(SERVER_URL)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .requestBody(jsonBody)
            .method(org.jsoup.Connection.Method.POST)
            .ignoreContentType(true)
            .ignoreHttpErrors(true)
            .timeout(TIMEOUT_MS)
            .execute();

        var statusCode = resp.statusCode();
        if (statusCode !== 200) {
            // 서버 에러 시 조용히 무시
            return;
        }

        var body = resp.body();
        var JSONObject = Java.type("org.json.JSONObject");
        var result = new JSONObject(body);

        var shouldReply = result.optBoolean("should_reply", false);
        var reply = result.optString("reply", "");

        if (shouldReply && reply) {
            replier.reply(reply);
        }
    } catch (e) {
        // 에러 시 조용히 무시 (사용자 경험 해치지 않음)
    }
}
