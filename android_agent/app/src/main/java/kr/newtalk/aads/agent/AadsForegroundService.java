package kr.newtalk.aads.agent;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;

public final class AadsForegroundService extends Service implements AadsWebSocketClient.Listener {
    private static final String TAG = "AadsForegroundService";

    static final String ACTION_START = "kr.newtalk.aads.agent.action.START";
    static final String ACTION_STOP = "kr.newtalk.aads.agent.action.STOP";
    static final String ACTION_STATE_CHANGED = "kr.newtalk.aads.agent.action.STATE_CHANGED";
    static final String ACTION_NETWORK_RESTORED = "kr.newtalk.aads.agent.action.NETWORK_RESTORED";
    static final String ACTION_WATCHDOG_RECONNECT = "kr.newtalk.aads.agent.action.WATCHDOG_RECONNECT";

    private static final String CHANNEL_ID = "aads_agent_connection";
    private static final int NOTIFICATION_ID = 231;
    private static final long WATCHDOG_INTERVAL_MS = 90_000;
    private static final long HEARTBEAT_TIMEOUT_MS = 120_000;

    private AadsWebSocketClient client;
    private String currentStatus = AgentStateStore.STATUS_DISCONNECTED;
    private String currentError = "";
    private String activeCommand = "";

    private final Handler watchdogHandler = new Handler(Looper.getMainLooper());
    private long lastHeartbeatMs = 0;

    private final Runnable watchdogRunnable = new Runnable() {
        @Override
        public void run() {
            if (client == null) return;
            if (lastHeartbeatMs > 0
                    && System.currentTimeMillis() - lastHeartbeatMs > HEARTBEAT_TIMEOUT_MS
                    && AgentStateStore.STATUS_CONNECTED.equals(currentStatus)) {
                Log.w(TAG, "Watchdog: heartbeat timeout — forcing reconnect");
                client.nudgeReconnect();
            }
            watchdogHandler.postDelayed(this, WATCHDOG_INTERVAL_MS);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        AgentStateStore.setStatus(this, AgentStateStore.STATUS_DISCONNECTED, "");
        AgentStateStore.setActiveCommand(this, "");
        requestBatteryOptimizationExemption();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopWatchdog();
            stopClient();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_NETWORK_RESTORED.equals(action)) {
            if (client == null) {
                Log.i(TAG, "Network restored — client missing, starting foreground client");
                startForegroundWithType(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
                startClient();
                startWatchdog();
            } else if (AgentStateStore.STATUS_DISCONNECTED.equals(currentStatus)) {
                Log.i(TAG, "Network restored — nudging immediate reconnect");
                client.nudgeReconnect();
            }
            return START_STICKY;
        }
        if (ACTION_WATCHDOG_RECONNECT.equals(action)) {
            if (client == null) {
                startForegroundWithType(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
                startClient();
                startWatchdog();
            } else {
                client.nudgeReconnect();
            }
            return START_STICKY;
        }
        startForegroundWithType(ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        startClient();
        startWatchdog();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopWatchdog();
        stopClient();
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        scheduleServiceRestart("task_removed");
        super.onTaskRemoved(rootIntent);
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
        lastHeartbeatMs = timestampMs;
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

    private void startWatchdog() {
        watchdogHandler.removeCallbacks(watchdogRunnable);
        watchdogHandler.postDelayed(watchdogRunnable, WATCHDOG_INTERVAL_MS);
    }

    private void stopWatchdog() {
        watchdogHandler.removeCallbacks(watchdogRunnable);
    }

    private void requestBatteryOptimizationExemption() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                Intent exemption = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                exemption.setData(Uri.parse("package:" + getPackageName()));
                exemption.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(exemption);
            } catch (Exception e) {
                Log.w(TAG, "Battery optimization exemption request failed", e);
            }
        }
    }

    private void scheduleServiceRestart(String reason) {
        AgentConfig config = AgentPrefs.load(this);
        if (!config.isPairingReady()) {
            Log.i(TAG, "Skip restart schedule (" + reason + "): pairing not ready");
            return;
        }
        Intent restart = new Intent(getApplicationContext(), AadsForegroundService.class);
        restart.setAction(ACTION_START);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pendingIntent = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? PendingIntent.getForegroundService(getApplicationContext(), 1, restart, flags)
                : PendingIntent.getService(getApplicationContext(), 1, restart, flags);
        AlarmManager alarmManager = (AlarmManager) getSystemService(ALARM_SERVICE);
        long triggerAt = System.currentTimeMillis() + 2_000L;
        try {
            if (alarmManager != null) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    alarmManager.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pendingIntent);
                } else {
                    alarmManager.setExact(AlarmManager.RTC_WAKEUP, triggerAt, pendingIntent);
                }
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(restart);
            } else {
                startService(restart);
            }
            Log.i(TAG, "Scheduled foreground service restart: " + reason);
        } catch (Exception e) {
            Log.w(TAG, "Failed to schedule service restart: " + reason, e);
        }
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
        } else if ("audio_record".equals(commandType)) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
                    && PermissionGate.has(this, android.Manifest.permission.RECORD_AUDIO)) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
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
