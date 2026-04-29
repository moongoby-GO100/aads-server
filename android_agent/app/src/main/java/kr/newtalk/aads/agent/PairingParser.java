package kr.newtalk.aads.agent;

import android.net.Uri;

import org.json.JSONException;
import org.json.JSONObject;

final class PairingParser {
    private PairingParser() {
    }

    static PairingData parse(String raw, AgentConfig fallback) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) {
            return new PairingData(fallback.serverUrl, fallback.agentId, fallback.token);
        }
        if (value.startsWith("{")) {
            PairingData parsed = parseJson(value, fallback);
            if (parsed != null) {
                return parsed;
            }
        }
        if (value.startsWith("wss://") || value.startsWith("ws://") || value.startsWith("https://")) {
            PairingData parsed = parseUri(value, fallback);
            if (parsed != null) {
                return parsed;
            }
        }
        return parseKeyValue(value, fallback);
    }

    private static PairingData parseJson(String value, AgentConfig fallback) {
        try {
            JSONObject obj = new JSONObject(value);
            String serverUrl = firstNonEmpty(
                    obj.optString("server_url"),
                    obj.optString("serverUrl"),
                    fallback.serverUrl
            );
            String agentId = firstNonEmpty(obj.optString("agent_id"), obj.optString("agentId"), fallback.agentId);
            String token = firstNonEmpty(obj.optString("token"), obj.optString("auth_token"), fallback.token);
            return new PairingData(serverUrl, agentId, token);
        } catch (JSONException ignored) {
            return null;
        }
    }

    private static PairingData parseUri(String value, AgentConfig fallback) {
        try {
            Uri uri = Uri.parse(value);
            String token = firstNonEmpty(uri.getQueryParameter("token"), fallback.token);
            String agentId = fallback.agentId;
            String path = uri.getPath();
            String serverUrl = fallback.serverUrl;
            if (path != null && !path.isEmpty()) {
                int lastSlash = path.lastIndexOf('/');
                String lastSegment = lastSlash >= 0 ? path.substring(lastSlash + 1) : path;
                if (!lastSegment.isEmpty() && !"ws".equals(lastSegment)) {
                    agentId = lastSegment;
                    String basePath = lastSlash <= 0 ? "" : path.substring(0, lastSlash);
                    serverUrl = uri.buildUpon().path(basePath).query(null).fragment(null).build().toString();
                } else {
                    serverUrl = uri.buildUpon().query(null).fragment(null).build().toString();
                }
            }
            return new PairingData(serverUrl, agentId, token);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static PairingData parseKeyValue(String value, AgentConfig fallback) {
        String serverUrl = fallback.serverUrl;
        String agentId = fallback.agentId;
        String token = fallback.token;
        String[] lines = value.split("[\\r\\n;&]+");
        for (String line : lines) {
            int idx = line.indexOf('=');
            if (idx <= 0) {
                continue;
            }
            String key = line.substring(0, idx).trim();
            String val = line.substring(idx + 1).trim();
            if ("server_url".equals(key) || "serverUrl".equals(key)) {
                serverUrl = val;
            } else if ("agent_id".equals(key) || "agentId".equals(key)) {
                agentId = val;
            } else if ("token".equals(key) || "auth_token".equals(key)) {
                token = val;
            }
        }
        return new PairingData(serverUrl, agentId, token);
    }

    private static String firstNonEmpty(String first, String second) {
        return firstNonEmpty(first, second, "");
    }

    private static String firstNonEmpty(String first, String second, String third) {
        if (first != null && !first.trim().isEmpty()) {
            return first.trim();
        }
        if (second != null && !second.trim().isEmpty()) {
            return second.trim();
        }
        return third == null ? "" : third.trim();
    }
}
