package kr.newtalk.aads.agent;

final class AgentConfig {
    static final String DEFAULT_SERVER_URL = "wss://aads.newtalk.kr/api/v1/devices/ws";
    static final String DEVICE_TYPE = "android";

    final String serverUrl;
    final String agentId;
    final String token;

    AgentConfig(String serverUrl, String agentId, String token) {
        this.serverUrl = serverUrl;
        this.agentId = agentId;
        this.token = token;
    }

    boolean isPairingReady() {
        return serverUrl != null
                && !serverUrl.trim().isEmpty()
                && agentId != null
                && !agentId.trim().isEmpty()
                && token != null
                && !token.trim().isEmpty();
    }
}
