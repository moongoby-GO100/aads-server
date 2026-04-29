package kr.newtalk.aads.agent;

final class AgentStateSnapshot {
    final String status;
    final long lastHeartbeatMs;
    final String activeCommand;
    final String lastError;

    AgentStateSnapshot(String status, long lastHeartbeatMs, String activeCommand, String lastError) {
        this.status = status == null ? AgentStateStore.STATUS_DISCONNECTED : status;
        this.lastHeartbeatMs = lastHeartbeatMs;
        this.activeCommand = activeCommand == null ? "" : activeCommand;
        this.lastError = lastError == null ? "" : lastError;
    }
}
