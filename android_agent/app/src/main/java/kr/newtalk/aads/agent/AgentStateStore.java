package kr.newtalk.aads.agent;

import android.content.Context;
import android.content.SharedPreferences;

final class AgentStateStore {
    static final String STATUS_DISCONNECTED = "disconnected";
    static final String STATUS_CONNECTING = "connecting";
    static final String STATUS_CONNECTED = "connected";

    private static final String PREFS = "aads_agent_state";
    private static final String KEY_STATUS = "status";
    private static final String KEY_LAST_HEARTBEAT = "last_heartbeat";
    private static final String KEY_ACTIVE_COMMAND = "active_command";
    private static final String KEY_LAST_ERROR = "last_error";

    private AgentStateStore() {
    }

    static void setStatus(Context context, String status, String lastError) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_STATUS, status)
                .putString(KEY_LAST_ERROR, lastError == null ? "" : lastError)
                .apply();
    }

    static void setLastHeartbeat(Context context, long timestampMs) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putLong(KEY_LAST_HEARTBEAT, timestampMs)
                .apply();
    }

    static void setActiveCommand(Context context, String command) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(KEY_ACTIVE_COMMAND, command == null ? "" : command)
                .apply();
    }

    static AgentStateSnapshot load(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return new AgentStateSnapshot(
                prefs.getString(KEY_STATUS, STATUS_DISCONNECTED),
                prefs.getLong(KEY_LAST_HEARTBEAT, 0L),
                prefs.getString(KEY_ACTIVE_COMMAND, ""),
                prefs.getString(KEY_LAST_ERROR, "")
        );
    }
}
