package kr.newtalk.aads.agent;

final class VoiceWakeState {
    final boolean enabled;
    final String status;
    final String lastError;
    final String lastText;
    final long lastWakeMs;

    VoiceWakeState(boolean enabled, String status, String lastError, String lastText, long lastWakeMs) {
        this.enabled = enabled;
        this.status = status == null ? "" : status;
        this.lastError = lastError == null ? "" : lastError;
        this.lastText = lastText == null ? "" : lastText;
        this.lastWakeMs = lastWakeMs;
    }
}
