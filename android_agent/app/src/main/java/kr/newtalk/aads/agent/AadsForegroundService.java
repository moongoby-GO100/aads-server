package kr.newtalk.aads.agent;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

public final class AadsForegroundService extends Service implements AadsWebSocketClient.Listener {
    static final String ACTION_START = "kr.newtalk.aads.agent.action.START";
    static final String ACTION_STOP = "kr.newtalk.aads.agent.action.STOP";
    static final String ACTION_STATE_CHANGED = "kr.newtalk.aads.agent.action.STATE_CHANGED";

    private static final String CHANNEL_ID = "aads_agent_connection";
    private static final int NOTIFICATION_ID = 231;

    private AadsWebSocketClient client;
    private String currentStatus = AgentStateStore.STATUS_DISCONNECTED;
    private String currentError = "";
    private String activeCommand = "";

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        AgentStateStore.setStatus(this, AgentStateStore.STATUS_DISCONNECTED, "");
        AgentStateStore.setActiveCommand(this, "");
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopClient();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        startForegroundWithType(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        startClient();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopClient();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onState(String status, String error) {
        currentStatus = status;
        currentError = error == null ? "" : error;
        AgentStateStore.setStatus(this, currentStatus, currentError);
        updateNotification();
        broadcastState();
    }

    @Override
    public void onHeartbeat(long timestampMs) {
        AgentStateStore.setLastHeartbeat(this, timestampMs);
        broadcastState();
    }

    @Override
    public void onCommandState(String commandType) {
        activeCommand = commandType == null ? "" : commandType;
        AgentStateStore.setActiveCommand(this, activeCommand);
        promoteForegroundTypeForCommand(activeCommand);
        updateNotification();
        broadcastState();
    }

    private void startClient() {
        if (client != null) {
            return;
        }
        AgentConfig config = AgentPrefs.load(this);
        client = new AadsWebSocketClient(this, config, this);
        client.start();
    }

    private void stopClient() {
        if (client != null) {
            client.stop();
            client = null;
        }
        currentStatus = AgentStateStore.STATUS_DISCONNECTED;
        currentError = "";
        activeCommand = "";
        AgentStateStore.setStatus(this, currentStatus, "");
        AgentStateStore.setActiveCommand(this, "");
        broadcastState();
    }

    private void promoteForegroundTypeForCommand(String commandType) {
        int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
        if ("camera".equals(commandType) || "camera_photo".equals(commandType)) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                    && PermissionGate.has(this, android.Manifest.permission.CAMERA)) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
            }
        } else if ("location".equals(commandType)) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && PermissionGate.hasAnyLocation(this)) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
            }
        }
        startForegroundWithType(type);
    }

    private void startForegroundWithType(int type) {
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, type);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void updateNotification() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, buildNotification());
        }
    }

    private Notification buildNotification() {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        String text = currentStatus;
        if (activeCommand != null && !activeCommand.isEmpty()) {
            text = text + " / " + activeCommand;
        } else if (currentError != null && !currentError.isEmpty()) {
            text = text + " / " + currentError;
        }

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return builder
                .setContentTitle("AADS Android Agent")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setOngoing(true)
                .setContentIntent(pendingIntent)
                .build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "AADS Agent Connection",
                NotificationManager.IMPORTANCE_LOW
        );
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    private void broadcastState() {
        Intent intent = new Intent(ACTION_STATE_CHANGED);
        intent.setPackage(getPackageName());
        sendBroadcast(intent);
    }
}
