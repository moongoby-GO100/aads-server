package kr.newtalk.aads.agent;

import android.content.Context;
import android.net.Uri;
import android.os.Build;

import org.json.JSONObject;

import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

final class AadsWebSocketClient {
    interface Listener {
        void onState(String status, String error);

        void onHeartbeat(long timestampMs);

        void onCommandState(String commandType);
    }

    private static final int[] BACKOFF_SECONDS = new int[]{5, 10, 20, 40, 60};

    private final Context context;
    private final AgentConfig config;
    private final CommandDispatcher dispatcher;
    private final Listener listener;
    private final OkHttpClient okHttpClient;
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
    private final ScheduledExecutorService commandExecutor = Executors.newSingleThreadScheduledExecutor();
    private final AtomicBoolean running = new AtomicBoolean(false);

    private WebSocket webSocket;
    private ScheduledFuture<?> heartbeatFuture;
    private ScheduledFuture<?> reconnectFuture;
    private int attempt;

    AadsWebSocketClient(Context context, AgentConfig config, Listener listener) {
        this.context = context.getApplicationContext();
        this.config = config;
        this.listener = listener;
        this.dispatcher = CommandDispatcher.create(this.context);
        this.okHttpClient = new OkHttpClient.Builder()
                .pingInterval(20, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .build();
    }

    void start() {
        if (!config.isPairingReady()) {
            listener.onState(AgentStateStore.STATUS_DISCONNECTED, "pairing server URL, agent ID, and token are required");
            return;
        }
        if (!running.compareAndSet(false, true)) {
            return;
        }
        connectNow();
    }

    void stop() {
        running.set(false);
        cancelHeartbeat();
        if (reconnectFuture != null) {
            reconnectFuture.cancel(true);
        }
        if (webSocket != null) {
            webSocket.close(1000, "stopped");
            webSocket = null;
        }
        okHttpClient.dispatcher().executorService().shutdown();
        scheduler.shutdownNow();
        commandExecutor.shutdownNow();
    }

    private void connectNow() {
        if (!running.get()) {
            return;
        }
        listener.onState(AgentStateStore.STATUS_CONNECTING, "");
        try {
            Request request = new Request.Builder()
                    .url(buildWebSocketUrl())
                    .build();
            webSocket = okHttpClient.newWebSocket(request, new AgentWebSocketListener());
        } catch (IllegalArgumentException e) {
            scheduleReconnect("invalid server URL: " + e.getMessage());
        }
    }

    private String buildWebSocketUrl() {
        String base = AgentPrefs.normalizeServerUrl(config.serverUrl);
        return base
                + "/"
                + Uri.encode(config.agentId)
                + "?token="
                + Uri.encode(config.token)
                + "&device_type="
                + AgentConfig.DEVICE_TYPE;
    }

    private void sendRegister(WebSocket socket) {
        JSONObject payload = new JSONObject();
        ResultJson.put(payload, "agent_id", config.agentId);
        ResultJson.put(payload, "device_type", AgentConfig.DEVICE_TYPE);
        ResultJson.put(payload, "hostname", Build.MANUFACTURER + " " + Build.MODEL);
        ResultJson.put(payload, "os_info", "Android " + Build.VERSION.RELEASE + " SDK " + Build.VERSION.SDK_INT);
        ResultJson.put(payload, "capabilities", dispatcher.capabilities());

        JSONObject msg = new JSONObject();
        ResultJson.put(msg, "type", "register");
        ResultJson.put(msg, "id", UUID.randomUUID().toString());
        ResultJson.put(msg, "payload", payload);
        socket.send(msg.toString());
    }

    private void scheduleHeartbeat(WebSocket socket) {
        cancelHeartbeat();
        heartbeatFuture = scheduler.scheduleAtFixedRate(() -> {
            if (!running.get()) {
                return;
            }
            JSONObject msg = new JSONObject();
            ResultJson.put(msg, "type", "heartbeat");
            ResultJson.put(msg, "id", UUID.randomUUID().toString());
            ResultJson.put(msg, "payload", new JSONObject());
            socket.send(msg.toString());
        }, 0, 25, TimeUnit.SECONDS);
    }

    private void cancelHeartbeat() {
        if (heartbeatFuture != null) {
            heartbeatFuture.cancel(true);
            heartbeatFuture = null;
        }
    }

    private void scheduleReconnect(String reason) {
        if (!running.get()) {
            return;
        }
        cancelHeartbeat();
        listener.onState(AgentStateStore.STATUS_DISCONNECTED, reason);
        int delay = BACKOFF_SECONDS[Math.min(attempt, BACKOFF_SECONDS.length - 1)];
        attempt++;
        reconnectFuture = scheduler.schedule(this::connectNow, delay, TimeUnit.SECONDS);
    }

    private void handleCommand(WebSocket socket, JSONObject msg) {
        String commandId = msg.optString("id", UUID.randomUUID().toString());
        JSONObject payload = msg.optJSONObject("payload");
        if (payload == null) {
            payload = new JSONObject();
        }
        String commandType = payload.optString("command_type", "");
        JSONObject params = payload.optJSONObject("params");
        listener.onCommandState(commandType);
        try {
            JSONObject result = dispatcher.dispatch(commandType, params);
            JSONObject response = new JSONObject();
            ResultJson.put(response, "type", "result");
            ResultJson.put(response, "id", commandId);
            ResultJson.put(response, "payload", result);
            if (!socket.send(response.toString())) {
                listener.onState(AgentStateStore.STATUS_CONNECTED, "result send queue rejected");
            }
        } catch (Exception e) {
            listener.onState(AgentStateStore.STATUS_CONNECTED, "result send failed: " + e.getMessage());
        } finally {
            listener.onCommandState("");
        }
    }

    private final class AgentWebSocketListener extends WebSocketListener {
        @Override
        public void onOpen(WebSocket socket, Response response) {
            attempt = 0;
            sendRegister(socket);
            scheduleHeartbeat(socket);
        }

        @Override
        public void onMessage(WebSocket socket, String text) {
            try {
                JSONObject msg = new JSONObject(text);
                String type = msg.optString("type", "");
                if ("registered".equals(type)) {
                    listener.onState(AgentStateStore.STATUS_CONNECTED, "");
                } else if ("heartbeat".equals(type)) {
                    listener.onHeartbeat(System.currentTimeMillis());
                } else if ("command".equals(type)) {
                    commandExecutor.execute(() -> handleCommand(socket, msg));
                }
            } catch (Exception e) {
                listener.onState(AgentStateStore.STATUS_CONNECTED, "invalid message: " + e.getMessage());
            }
        }

        @Override
        public void onClosing(WebSocket socket, int code, String reason) {
            socket.close(code, reason);
        }

        @Override
        public void onClosed(WebSocket socket, int code, String reason) {
            scheduleReconnect(reason == null || reason.isEmpty() ? "closed" : reason);
        }

        @Override
        public void onFailure(WebSocket socket, Throwable t, Response response) {
            String message = t == null || t.getMessage() == null ? "websocket failure" : t.getMessage();
            scheduleReconnect(message);
        }
    }
}
