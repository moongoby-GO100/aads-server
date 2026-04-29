package kr.newtalk.aads.agent;

import org.json.JSONException;
import org.json.JSONObject;

final class ResultJson {
    private ResultJson() {
    }

    static JSONObject success(JSONObject data) {
        return envelope("success", data == null ? new JSONObject() : data);
    }

    static JSONObject error(String message) {
        JSONObject data = new JSONObject();
        put(data, "error", message);
        return envelope("error", data);
    }

    static JSONObject timeout(String message) {
        JSONObject data = new JSONObject();
        put(data, "error", message);
        return envelope("timeout", data);
    }

    static JSONObject permissionError(String permission, String commandType) {
        JSONObject data = new JSONObject();
        put(data, "error", "permission required");
        put(data, "permission", permission);
        put(data, "command_type", commandType);
        put(data, "user_visible_state", "permission_required");
        return envelope("error", data);
    }

    static void put(JSONObject obj, String key, Object value) {
        try {
            obj.put(key, value);
        } catch (JSONException ignored) {
        }
    }

    private static JSONObject envelope(String status, JSONObject data) {
        JSONObject obj = new JSONObject();
        put(obj, "status", status);
        put(obj, "data", data);
        return obj;
    }
}
