package kr.newtalk.aads.agent;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.UUID;

final class AgentPrefs {
    private static final String PREFS = "aads_agent_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_AGENT_ID = "agent_id";
    private static final String KEY_TOKEN = "token";

    private AgentPrefs() {
    }

    static AgentConfig load(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String agentId = prefs.getString(KEY_AGENT_ID, "");
        if (agentId == null || agentId.trim().isEmpty()) {
            agentId = newAgentId();
            prefs.edit().putString(KEY_AGENT_ID, agentId).apply();
        }
        String serverUrl = prefs.getString(KEY_SERVER_URL, AgentConfig.DEFAULT_SERVER_URL);
        String token = prefs.getString(KEY_TOKEN, "");
        return new AgentConfig(
                serverUrl == null ? AgentConfig.DEFAULT_SERVER_URL : serverUrl,
                agentId,
                token == null ? "" : token
        );
    }

    static void save(Context context, String serverUrl, String agentId, String token) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_SERVER_URL, normalizeServerUrl(serverUrl))
                .putString(KEY_AGENT_ID, emptyToGenerated(agentId))
                .putString(KEY_TOKEN, token == null ? "" : token.trim())
                .apply();
    }

    static String regenerateAgentId(Context context) {
        String next = newAgentId();
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_AGENT_ID, next)
                .apply();
        return next;
    }

    static String normalizeServerUrl(String serverUrl) {
        String value = serverUrl == null ? "" : serverUrl.trim();
        if (value.isEmpty()) {
            return AgentConfig.DEFAULT_SERVER_URL;
        }
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        return value;
    }

    private static String emptyToGenerated(String agentId) {
        String value = agentId == null ? "" : agentId.trim();
        return value.isEmpty() ? newAgentId() : value;
    }

    private static String newAgentId() {
        return UUID.randomUUID().toString().replace("-", "").substring(0, 12);
    }
}
