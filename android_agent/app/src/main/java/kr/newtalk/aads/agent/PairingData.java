package kr.newtalk.aads.agent;

final class PairingData {
    final String serverUrl;
    final String agentId;
    final String token;

    PairingData(String serverUrl, String agentId, String token) {
        this.serverUrl = serverUrl == null ? "" : serverUrl.trim();
        this.agentId = agentId == null ? "" : agentId.trim();
        this.token = token == null ? "" : token.trim();
    }
}
