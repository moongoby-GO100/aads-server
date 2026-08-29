package kr.newtalk.aads.agent;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.admin.DevicePolicyManager;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureFailure;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.location.Location;
import android.location.LocationManager;
import android.media.AudioManager;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaRecorder;
import android.net.Uri;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.PowerManager;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.provider.CallLog;
import android.provider.ContactsContract;
import android.provider.MediaStore;
import android.provider.Settings;
import android.provider.Telephony;
import android.speech.tts.TextToSpeech;
import android.telephony.SmsManager;
import android.util.Base64;
import android.util.Size;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class AndroidCommandHandlers {
    private static final String NOTIFICATION_CHANNEL_ID = "aads_agent_commands";
    private static final Pattern TOKEN_PATTERN = Pattern.compile("\"([^\"]*)\"|'([^']*)'|\\S+");
    private static final Pattern SAFE_ARG_PATTERN = Pattern.compile("[A-Za-z0-9._:/=-]+");

    private static final Object TTS_LOCK = new Object();
    private static TextToSpeech textToSpeech;
    private static boolean textToSpeechReady;

    private AndroidCommandHandlers() {
    }

    static JSONObject battery(Context context) {
        Intent battery = context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        JSONObject data = new JSONObject();
        if (battery == null) {
            return ResultJson.error("battery status unavailable");
        }
        int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        int percent = scale > 0 && level >= 0 ? Math.round((level * 100f) / scale) : -1;
        ResultJson.put(data, "level", level);
        ResultJson.put(data, "scale", scale);
        ResultJson.put(data, "percent", percent);
        ResultJson.put(data, "status", battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1));
        ResultJson.put(data, "plugged", battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1));
        ResultJson.put(data, "health", battery.getIntExtra(BatteryManager.EXTRA_HEALTH, -1));
        ResultJson.put(data, "temperature_c", battery.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0);
        ResultJson.put(data, "voltage_mv", battery.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1));
        return ResultJson.success(data);
    }

    static JSONObject permissionStatus(Context context) {
        JSONObject data = new JSONObject();
        JSONArray runtime = new JSONArray();
        addPermissionStatus(runtime, context, Manifest.permission.CAMERA, "camera", "camera");
        addPermissionStatus(runtime, context, Manifest.permission.SEND_SMS, "send_sms", "sms_send");
        addPermissionStatus(runtime, context, Manifest.permission.READ_SMS, "read_sms", "sms_inbox");
        addPermissionStatus(runtime, context, Manifest.permission.READ_CONTACTS, "read_contacts", "contacts");
        addPermissionStatus(runtime, context, Manifest.permission.READ_CALL_LOG, "read_call_log", "call_log");
        addPermissionStatus(runtime, context, Manifest.permission.RECORD_AUDIO, "record_audio", "audio_record");
        addPermissionStatus(runtime, context, Manifest.permission.ACCESS_FINE_LOCATION, "fine_location", "location");
        addPermissionStatus(runtime, context, Manifest.permission.ACCESS_COARSE_LOCATION, "coarse_location", "location");
        if (Build.VERSION.SDK_INT >= 33) {
            addPermissionStatus(runtime, context, Manifest.permission.POST_NOTIFICATIONS, "post_notifications", "notification");
            addPermissionStatus(runtime, context, Manifest.permission.NEARBY_WIFI_DEVICES, "nearby_wifi_devices", "wifi");
            addPermissionStatus(runtime, context, Manifest.permission.READ_MEDIA_IMAGES, "read_media_images", "photo_gallery");
        } else {
            addPermissionStatus(runtime, context, Manifest.permission.READ_EXTERNAL_STORAGE, "read_external_storage", "photo_gallery");
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            addPermissionStatus(runtime, context, Manifest.permission.BLUETOOTH_CONNECT, "bluetooth_connect", "bluetooth_status");
        }

        JSONObject special = new JSONObject();
        ResultJson.put(special, "accessibility_enabled", isAccessibilityEnabled(context));
        ResultJson.put(special, "accessibility_service_running", AadsAccessibilityService.isReady());
        ResultJson.put(special, "notification_listener_enabled", AadsNotificationListener.isEnabled(context));
        ResultJson.put(special, "device_admin_active", AadsDeviceAdminReceiver.isAdminActive(context));
        ResultJson.put(special, "write_settings_allowed", Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.System.canWrite(context));
        ResultJson.put(special, "battery_optimization_ignored", isBatteryOptimizationIgnored(context));

        ResultJson.put(data, "package", context.getPackageName());
        ResultJson.put(data, "sdk_int", Build.VERSION.SDK_INT);
        ResultJson.put(data, "runtime_permissions", runtime);
        ResultJson.put(data, "special_permissions", special);
        ResultJson.put(data, "all_runtime_granted", allRuntimeGranted(runtime));
        ResultJson.put(data, "all_special_ready", allSpecialReady(special));
        return ResultJson.success(data);
    }

    static JSONObject location(Context context, JSONObject params) {
        if (!PermissionGate.hasAnyLocation(context)) {
            return ResultJson.permissionError(Manifest.permission.ACCESS_FINE_LOCATION, "location");
        }
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) {
            return ResultJson.error("location manager unavailable");
        }
        String provider = params.optString("provider", "best");
        try {
            Location location;
            if ("gps".equals(provider) || "network".equals(provider) || "passive".equals(provider)) {
                location = manager.getLastKnownLocation(provider);
            } else {
                location = bestLastKnownLocation(manager);
            }
            if (location == null) {
                return ResultJson.error("no last known location available");
            }
            return ResultJson.success(locationToJson(location));
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.ACCESS_FINE_LOCATION, "location");
        }
    }

    static JSONObject camera(Context context, JSONObject params) throws Exception {
        if (!PermissionGate.has(context, Manifest.permission.CAMERA)) {
            return ResultJson.permissionError(Manifest.permission.CAMERA, "camera");
        }
        CameraManager manager = (CameraManager) context.getSystemService(Context.CAMERA_SERVICE);
        if (manager == null) {
            return ResultJson.error("camera manager unavailable");
        }
        String cameraId = params.optString("camera_id", "");
        if (cameraId.isEmpty()) {
            cameraId = findBackCamera(manager);
        }
        if (cameraId == null || cameraId.isEmpty()) {
            return ResultJson.error("no camera available");
        }

        CameraCharacteristics characteristics = manager.getCameraCharacteristics(cameraId);
        Size captureSize = chooseCaptureSize(characteristics, params.optInt("max_width", 640), params.optInt("max_height", 480));
        HandlerThread thread = new HandlerThread("AadsCameraCapture");
        thread.start();
        Handler handler = new Handler(thread.getLooper());
        ImageReader reader = ImageReader.newInstance(captureSize.getWidth(), captureSize.getHeight(), android.graphics.ImageFormat.JPEG, 1);
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<byte[]> bytesRef = new AtomicReference<>();
        AtomicReference<Exception> errorRef = new AtomicReference<>();
        AtomicReference<CameraDevice> deviceRef = new AtomicReference<>();
        AtomicReference<CameraCaptureSession> sessionRef = new AtomicReference<>();

        reader.setOnImageAvailableListener(imageReader -> {
            Image image = null;
            try {
                image = imageReader.acquireLatestImage();
                if (image == null) {
                    errorRef.set(new IllegalStateException("camera produced no image"));
                    return;
                }
                ByteBuffer buffer = image.getPlanes()[0].getBuffer();
                byte[] bytes = new byte[buffer.remaining()];
                buffer.get(bytes);
                bytesRef.set(bytes);
            } catch (Exception e) {
                errorRef.set(e);
            } finally {
                if (image != null) {
                    image.close();
                }
                latch.countDown();
            }
        }, handler);

        try {
            String finalCameraId = cameraId;
            manager.openCamera(finalCameraId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice camera) {
                    deviceRef.set(camera);
                    try {
                        camera.createCaptureSession(Collections.singletonList(reader.getSurface()), new CameraCaptureSession.StateCallback() {
                            @Override
                            public void onConfigured(CameraCaptureSession session) {
                                sessionRef.set(session);
                                try {
                                    CaptureRequest.Builder request = camera.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
                                    request.addTarget(reader.getSurface());
                                    request.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
                                    session.capture(request.build(), new CameraCaptureSession.CaptureCallback() {
                                        @Override
                                        public void onCaptureCompleted(CameraCaptureSession session, CaptureRequest request, TotalCaptureResult result) {
                                        }

                                        @Override
                                        public void onCaptureFailed(CameraCaptureSession session, CaptureRequest request, CaptureFailure failure) {
                                            errorRef.set(new IllegalStateException("camera capture failed: " + failure.getReason()));
                                            latch.countDown();
                                        }
                                    }, handler);
                                } catch (Exception e) {
                                    errorRef.set(e);
                                    latch.countDown();
                                }
                            }

                            @Override
                            public void onConfigureFailed(CameraCaptureSession session) {
                                errorRef.set(new IllegalStateException("camera session configuration failed"));
                                latch.countDown();
                            }
                        }, handler);
                    } catch (CameraAccessException e) {
                        errorRef.set(e);
                        latch.countDown();
                    }
                }

                @Override
                public void onDisconnected(CameraDevice camera) {
                    camera.close();
                    errorRef.set(new IllegalStateException("camera disconnected"));
                    latch.countDown();
                }

                @Override
                public void onError(CameraDevice camera, int error) {
                    camera.close();
                    errorRef.set(new IllegalStateException("camera error: " + error));
                    latch.countDown();
                }
            }, handler);

            int timeoutSeconds = Math.max(3, Math.min(params.optInt("timeout_seconds", 10), 20));
            boolean completed = latch.await(timeoutSeconds, TimeUnit.SECONDS);
            if (!completed) {
                return ResultJson.timeout("camera capture timed out");
            }
            if (errorRef.get() != null) {
                return ResultJson.error(errorRef.get().getMessage());
            }
            byte[] bytes = bytesRef.get();
            if (bytes == null || bytes.length == 0) {
                return ResultJson.error("camera image unavailable");
            }
            String base64 = Base64.encodeToString(bytes, Base64.NO_WRAP);
            int maxChars = Math.max(128, Math.min(params.optInt("max_base64_chars", 2000), 20000));
            JSONObject data = new JSONObject();
            ResultJson.put(data, "camera_id", cameraId);
            ResultJson.put(data, "width", captureSize.getWidth());
            ResultJson.put(data, "height", captureSize.getHeight());
            ResultJson.put(data, "bytes", bytes.length);
            ResultJson.put(data, "base64", base64.length() > maxChars ? base64.substring(0, maxChars) + "...(truncated)" : base64);
            return ResultJson.success(data);
        } finally {
            CameraCaptureSession session = sessionRef.get();
            if (session != null) {
                session.close();
            }
            CameraDevice device = deviceRef.get();
            if (device != null) {
                device.close();
            }
            reader.close();
            thread.quitSafely();
        }
    }

    static JSONObject notification(Context context, JSONObject params) {
        if (!PermissionGate.hasNotification(context)) {
            return ResultJson.permissionError(Manifest.permission.POST_NOTIFICATIONS, "notification");
        }
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return ResultJson.error("notification manager unavailable");
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(new NotificationChannel(
                    NOTIFICATION_CHANNEL_ID,
                    "AADS Agent Commands",
                    NotificationManager.IMPORTANCE_DEFAULT
            ));
        }
        String title = params.optString("title", "AADS");
        String content = params.optString("content", params.optString("body", ""));
        int id = params.optInt("id", (int) (System.currentTimeMillis() % 100000));
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(context, NOTIFICATION_CHANNEL_ID)
                : new Notification.Builder(context);
        Notification notification = builder
                .setContentTitle(title)
                .setContentText(content)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setAutoCancel(true)
                .build();
        manager.notify(id, notification);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "id", id);
        ResultJson.put(data, "user_visible_state", "notification_posted");
        return ResultJson.success(data);
    }

    static JSONObject clipboard(Context context, JSONObject params, String forcedAction) {
        ClipboardManager clipboard = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) {
            return ResultJson.error("clipboard manager unavailable");
        }
        String action = forcedAction == null || forcedAction.isEmpty()
                ? params.optString("action", params.has("text") ? "set" : "get")
                : forcedAction;
        if ("set".equals(action)) {
            String text = params.optString("text", "");
            clipboard.setPrimaryClip(ClipData.newPlainText("AADS", text));
            JSONObject data = new JSONObject();
            ResultJson.put(data, "length", text.length());
            return ResultJson.success(data);
        }
        ClipData clip = clipboard.getPrimaryClip();
        String text = "";
        if (clip != null && clip.getItemCount() > 0 && clip.getItemAt(0).coerceToText(context) != null) {
            text = clip.getItemAt(0).coerceToText(context).toString();
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "text", text);
        return ResultJson.success(data);
    }

    static JSONObject vibrate(Context context, JSONObject params) {
        Vibrator vibrator;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            VibratorManager manager = (VibratorManager) context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
            vibrator = manager == null ? null : manager.getDefaultVibrator();
        } else {
            vibrator = (Vibrator) context.getSystemService(Context.VIBRATOR_SERVICE);
        }
        if (vibrator == null || !vibrator.hasVibrator()) {
            return ResultJson.error("vibrator unavailable");
        }
        long durationMs = Math.max(1, Math.min(params.optLong("duration_ms", 500), 5000));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            vibrator.vibrate(VibrationEffect.createOneShot(durationMs, VibrationEffect.DEFAULT_AMPLITUDE));
        } else {
            vibrator.vibrate(durationMs);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "duration_ms", durationMs);
        ResultJson.put(data, "user_visible_state", "vibrating");
        return ResultJson.success(data);
    }

    static JSONObject tts(Context context, JSONObject params) throws InterruptedException {
        String text = params.optString("text", "");
        if (text.trim().isEmpty()) {
            return ResultJson.error("text required");
        }
        TextToSpeech tts = getTextToSpeech(context);
        if (tts == null || !textToSpeechReady) {
            return ResultJson.error("text to speech unavailable");
        }
        String language = params.optString("language", params.optString("lang", ""));
        if (!language.isEmpty()) {
            tts.setLanguage(Locale.forLanguageTag(language));
        }
        int status = tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, UUID.randomUUID().toString());
        JSONObject data = new JSONObject();
        ResultJson.put(data, "queued", status == TextToSpeech.SUCCESS);
        ResultJson.put(data, "length", text.length());
        ResultJson.put(data, "user_visible_state", "tts_speaking");
        return status == TextToSpeech.SUCCESS ? ResultJson.success(data) : ResultJson.error("tts speak failed");
    }

    static JSONObject volume(Context context, JSONObject params) {
        AudioManager manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        if (manager == null) {
            return ResultJson.error("audio manager unavailable");
        }
        int stream = streamType(params.optString("stream", "music"));
        int max = manager.getStreamMaxVolume(stream);
        int current = manager.getStreamVolume(stream);
        if (params.has("volume")) {
            int volume = Math.max(0, Math.min(params.optInt("volume", current), max));
            manager.setStreamVolume(stream, volume, AudioManager.FLAG_SHOW_UI);
            current = volume;
        } else if (params.has("delta")) {
            int volume = Math.max(0, Math.min(current + params.optInt("delta", 0), max));
            manager.setStreamVolume(stream, volume, AudioManager.FLAG_SHOW_UI);
            current = volume;
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "stream", params.optString("stream", "music"));
        ResultJson.put(data, "volume", current);
        ResultJson.put(data, "max", max);
        ResultJson.put(data, "user_visible_state", "volume_ui_shown");
        return ResultJson.success(data);
    }

    static JSONObject wifi(Context context, JSONObject params, String forcedAction) {
        WifiManager manager = (WifiManager) context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (manager == null) {
            return ResultJson.error("wifi manager unavailable");
        }
        String action = forcedAction == null || forcedAction.isEmpty()
                ? params.optString("action", "info")
                : forcedAction;
        if ("scan".equals(action)) {
            if (!PermissionGate.hasNearbyWifi(context) || !PermissionGate.hasAnyLocation(context)) {
                String permission = Build.VERSION.SDK_INT >= 33
                        ? Manifest.permission.NEARBY_WIFI_DEVICES
                        : Manifest.permission.ACCESS_FINE_LOCATION;
                return ResultJson.permissionError(permission, "wifi");
            }
            JSONArray networks = new JSONArray();
            try {
                for (ScanResult result : manager.getScanResults()) {
                    JSONObject item = new JSONObject();
                    ResultJson.put(item, "ssid", result.SSID);
                    ResultJson.put(item, "bssid", result.BSSID);
                    ResultJson.put(item, "level", result.level);
                    ResultJson.put(item, "frequency", result.frequency);
                    ResultJson.put(item, "capabilities", result.capabilities);
                    networks.put(item);
                }
            } catch (SecurityException e) {
                return ResultJson.permissionError(Manifest.permission.ACCESS_FINE_LOCATION, "wifi");
            }
            JSONObject data = new JSONObject();
            ResultJson.put(data, "networks", networks);
            ResultJson.put(data, "count", networks.length());
            return ResultJson.success(data);
        }
        WifiInfo info = manager.getConnectionInfo();
        JSONObject data = new JSONObject();
        ResultJson.put(data, "enabled", manager.isWifiEnabled());
        if (info != null) {
            ResultJson.put(data, "ssid", stripQuotes(info.getSSID()));
            ResultJson.put(data, "bssid", info.getBSSID());
            ResultJson.put(data, "rssi", info.getRssi());
            ResultJson.put(data, "link_speed_mbps", info.getLinkSpeed());
            ResultJson.put(data, "network_id", info.getNetworkId());
        }
        return ResultJson.success(data);
    }

    static JSONObject shellLimited(JSONObject params) throws Exception {
        String command = params.optString("command", params.optString("cmd", ""));
        if (command.trim().isEmpty()) {
            return ResultJson.error("command required");
        }
        List<String> tokens = tokenizeCommand(command);
        if (!isAllowedShellCommand(tokens)) {
            return ResultJson.error("blocked shell_limited command");
        }
        int timeoutSeconds = Math.max(1, Math.min(params.optInt("timeout", 10), 30));
        Process process = new ProcessBuilder(tokens).redirectErrorStream(true).start();
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        Thread reader = new Thread(() -> copyCapped(process.getInputStream(), output, 12000), "AadsShellReader");
        reader.start();
        boolean completed = process.waitFor(timeoutSeconds, TimeUnit.SECONDS);
        if (!completed) {
            process.destroyForcibly();
            reader.join(500);
            return ResultJson.timeout("command timed out");
        }
        reader.join(1000);
        String text = output.toString(StandardCharsets.UTF_8.name());
        JSONObject data = new JSONObject();
        ResultJson.put(data, "stdout", text);
        ResultJson.put(data, "returncode", process.exitValue());
        ResultJson.put(data, "allowlist", true);
        return process.exitValue() == 0 ? ResultJson.success(data) : ResultJson.error("command failed: " + process.exitValue());
    }

    static JSONObject smsSend(Context context, JSONObject params) {
        if (!PermissionGate.has(context, Manifest.permission.SEND_SMS)) {
            return ResultJson.permissionError(Manifest.permission.SEND_SMS, "sms_send");
        }
        String number = params.optString("number", "");
        String body = params.optString("body", params.optString("message", ""));
        if (number.trim().isEmpty() || body.trim().isEmpty()) {
            return ResultJson.error("number and body required");
        }
        SmsManager smsManager = SmsManager.getDefault();
        smsManager.sendTextMessage(number, null, body, null, null);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "number", number);
        ResultJson.put(data, "length", body.length());
        ResultJson.put(data, "user_visible_state", "sms_requested");
        return ResultJson.success(data);
    }

    static JSONObject callDial(Context context, JSONObject params) {
        String number = params.optString("number", "");
        if (number.trim().isEmpty()) {
            return ResultJson.error("number required");
        }
        Intent intent = new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number)));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (intent.resolveActivity(context.getPackageManager()) == null) {
            return ResultJson.error("dialer unavailable");
        }
        context.startActivity(intent);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "number", number);
        ResultJson.put(data, "user_visible_state", "dialer_opened");
        return ResultJson.success(data);
    }

    static JSONObject contactsList(Context context, JSONObject params) {
        if (!PermissionGate.has(context, Manifest.permission.READ_CONTACTS)) {
            return ResultJson.permissionError(Manifest.permission.READ_CONTACTS, "contacts_list");
        }
        int limit = boundedInt(params, "limit", 100, 1, 1000);
        int offset = Math.max(0, params.optInt("offset", 0));
        JSONArray contacts = new JSONArray();
        String[] projection = new String[]{
                ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER
        };
        try (Cursor cursor = context.getContentResolver().query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                projection,
                null,
                null,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE LOCALIZED ASC"
        )) {
            appendContacts(cursor, contacts, limit, offset);
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.READ_CONTACTS, "contacts_list");
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "contacts", contacts);
        ResultJson.put(data, "count", contacts.length());
        ResultJson.put(data, "limit", limit);
        ResultJson.put(data, "offset", offset);
        return ResultJson.success(data);
    }

    static JSONObject contactsSearch(Context context, JSONObject params) {
        if (!PermissionGate.has(context, Manifest.permission.READ_CONTACTS)) {
            return ResultJson.permissionError(Manifest.permission.READ_CONTACTS, "contacts_search");
        }
        String name = params.optString("name", params.optString("query", "")).trim();
        if (name.isEmpty()) {
            return ResultJson.error("name required");
        }
        int limit = boundedInt(params, "limit", 100, 1, 1000);
        JSONArray contacts = new JSONArray();
        String[] projection = new String[]{
                ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER
        };
        String selection = ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " LIKE ?";
        String[] selectionArgs = new String[]{"%" + name + "%"};
        try (Cursor cursor = context.getContentResolver().query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                projection,
                selection,
                selectionArgs,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE LOCALIZED ASC"
        )) {
            appendContacts(cursor, contacts, limit, 0);
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.READ_CONTACTS, "contacts_search");
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "contacts", contacts);
        ResultJson.put(data, "count", contacts.length());
        ResultJson.put(data, "query", name);
        return ResultJson.success(data);
    }

    static JSONObject callLog(Context context, JSONObject params) {
        if (!PermissionGate.has(context, Manifest.permission.READ_CALL_LOG)) {
            return ResultJson.permissionError(Manifest.permission.READ_CALL_LOG, "call_log");
        }
        int limit = boundedInt(params, "limit", 50, 1, 500);
        JSONArray calls = new JSONArray();
        String[] projection = new String[]{
                CallLog.Calls.NUMBER,
                CallLog.Calls.TYPE,
                CallLog.Calls.DATE,
                CallLog.Calls.DURATION
        };
        try (Cursor cursor = context.getContentResolver().query(
                CallLog.Calls.CONTENT_URI,
                projection,
                null,
                null,
                CallLog.Calls.DATE + " DESC"
        )) {
            if (cursor != null) {
                while (cursor.moveToNext() && calls.length() < limit) {
                    JSONObject item = new JSONObject();
                    int type = cursor.getInt(cursor.getColumnIndexOrThrow(CallLog.Calls.TYPE));
                    ResultJson.put(item, "number", cursor.getString(cursor.getColumnIndexOrThrow(CallLog.Calls.NUMBER)));
                    ResultJson.put(item, "type", type);
                    ResultJson.put(item, "type_name", callTypeName(type));
                    ResultJson.put(item, "date", cursor.getLong(cursor.getColumnIndexOrThrow(CallLog.Calls.DATE)));
                    ResultJson.put(item, "duration", cursor.getLong(cursor.getColumnIndexOrThrow(CallLog.Calls.DURATION)));
                    calls.put(item);
                }
            }
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.READ_CALL_LOG, "call_log");
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "calls", calls);
        ResultJson.put(data, "count", calls.length());
        ResultJson.put(data, "limit", limit);
        return ResultJson.success(data);
    }

    static JSONObject smsInbox(Context context, JSONObject params) {
        if (!PermissionGate.has(context, Manifest.permission.READ_SMS)) {
            return ResultJson.permissionError(Manifest.permission.READ_SMS, "sms_inbox");
        }
        int limit = boundedInt(params, "limit", 50, 1, 500);
        JSONArray messages = new JSONArray();
        String[] projection = new String[]{
                Telephony.Sms.Inbox.ADDRESS,
                Telephony.Sms.Inbox.BODY,
                Telephony.Sms.Inbox.DATE
        };
        try (Cursor cursor = context.getContentResolver().query(
                Telephony.Sms.Inbox.CONTENT_URI,
                projection,
                null,
                null,
                Telephony.Sms.Inbox.DATE + " DESC"
        )) {
            if (cursor != null) {
                while (cursor.moveToNext() && messages.length() < limit) {
                    JSONObject item = new JSONObject();
                    ResultJson.put(item, "address", cursor.getString(cursor.getColumnIndexOrThrow(Telephony.Sms.Inbox.ADDRESS)));
                    ResultJson.put(item, "body", cursor.getString(cursor.getColumnIndexOrThrow(Telephony.Sms.Inbox.BODY)));
                    ResultJson.put(item, "date", cursor.getLong(cursor.getColumnIndexOrThrow(Telephony.Sms.Inbox.DATE)));
                    messages.put(item);
                }
            }
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.READ_SMS, "sms_inbox");
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "messages", messages);
        ResultJson.put(data, "count", messages.length());
        ResultJson.put(data, "limit", limit);
        return ResultJson.success(data);
    }

    static JSONObject photoGallery(Context context, JSONObject params) {
        if (!PermissionGate.hasImageRead(context)) {
            String permission = Build.VERSION.SDK_INT >= 33
                    ? Manifest.permission.READ_MEDIA_IMAGES
                    : Manifest.permission.READ_EXTERNAL_STORAGE;
            return ResultJson.permissionError(permission, "photo_gallery");
        }
        int limit = boundedInt(params, "limit", 20, 1, 500);
        JSONArray photos = new JSONArray();
        String[] projection = new String[]{
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DATA,
                MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.DATE_TAKEN,
                MediaStore.Images.Media.SIZE
        };
        try (Cursor cursor = context.getContentResolver().query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                projection,
                null,
                null,
                MediaStore.Images.Media.DATE_ADDED + " DESC"
        )) {
            if (cursor != null) {
                while (cursor.moveToNext() && photos.length() < limit) {
                    long id = cursor.getLong(cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID));
                    String path = cursor.getString(cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATA));
                    long dateTaken = cursor.getLong(cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN));
                    long dateAdded = cursor.getLong(cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_ADDED));
                    if (path == null || path.isEmpty()) {
                        Uri uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id);
                        path = uri.toString();
                    }
                    JSONObject item = new JSONObject();
                    ResultJson.put(item, "id", id);
                    ResultJson.put(item, "path", path);
                    ResultJson.put(item, "date", dateTaken > 0 ? dateTaken : dateAdded * 1000L);
                    ResultJson.put(item, "size", cursor.getLong(cursor.getColumnIndexOrThrow(MediaStore.Images.Media.SIZE)));
                    photos.put(item);
                }
            }
        } catch (SecurityException e) {
            String permission = Build.VERSION.SDK_INT >= 33
                    ? Manifest.permission.READ_MEDIA_IMAGES
                    : Manifest.permission.READ_EXTERNAL_STORAGE;
            return ResultJson.permissionError(permission, "photo_gallery");
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "photos", photos);
        ResultJson.put(data, "count", photos.length());
        ResultJson.put(data, "limit", limit);
        return ResultJson.success(data);
    }

    static JSONObject appList(Context context) {
        PackageManager packageManager = context.getPackageManager();
        JSONArray apps = new JSONArray();
        List<ApplicationInfo> installed = packageManager.getInstalledApplications(PackageManager.GET_META_DATA);
        Collections.sort(installed, Comparator.comparing(app -> app.packageName));
        for (ApplicationInfo app : installed) {
            JSONObject item = new JSONObject();
            ResultJson.put(item, "package", app.packageName);
            ResultJson.put(item, "label", String.valueOf(packageManager.getApplicationLabel(app)));
            try {
                PackageInfo info = packageManager.getPackageInfo(app.packageName, 0);
                ResultJson.put(item, "version", info.versionName == null ? "" : info.versionName);
                ResultJson.put(item, "version_code", Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                        ? info.getLongVersionCode()
                        : info.versionCode);
            } catch (PackageManager.NameNotFoundException ignored) {
                ResultJson.put(item, "version", "");
                ResultJson.put(item, "version_code", 0);
            }
            apps.put(item);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "apps", apps);
        ResultJson.put(data, "count", apps.length());
        return ResultJson.success(data);
    }

    static JSONObject appLaunch(Context context, JSONObject params) {
        String packageName = params.optString("package", params.optString("package_name", "")).trim();
        if (packageName.isEmpty()) {
            return ResultJson.error("package required");
        }
        Intent intent = context.getPackageManager().getLaunchIntentForPackage(packageName);
        if (intent == null) {
            return ResultJson.error("launch intent unavailable");
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(intent);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "package", packageName);
        ResultJson.put(data, "user_visible_state", "app_launched");
        return ResultJson.success(data);
    }

    static JSONObject bluetoothStatus(Context context, JSONObject params) {
        if (!PermissionGate.hasBluetoothConnect(context)) {
            return ResultJson.permissionError(Manifest.permission.BLUETOOTH_CONNECT, "bluetooth_status");
        }
        BluetoothManager manager = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        BluetoothAdapter adapter = manager == null ? BluetoothAdapter.getDefaultAdapter() : manager.getAdapter();
        if (adapter == null) {
            return ResultJson.error("bluetooth adapter unavailable");
        }
        JSONArray paired = new JSONArray();
        try {
            Set<BluetoothDevice> bondedDevices = adapter.getBondedDevices();
            if (bondedDevices != null) {
                for (BluetoothDevice device : bondedDevices) {
                    JSONObject item = new JSONObject();
                    ResultJson.put(item, "name", device.getName());
                    ResultJson.put(item, "address", device.getAddress());
                    ResultJson.put(item, "type", device.getType());
                    ResultJson.put(item, "bond_state", device.getBondState());
                    paired.put(item);
                }
            }
            JSONObject data = new JSONObject();
            ResultJson.put(data, "enabled", adapter.isEnabled());
            ResultJson.put(data, "state", adapter.getState());
            ResultJson.put(data, "state_name", bluetoothStateName(adapter.getState()));
            ResultJson.put(data, "paired_devices", paired);
            ResultJson.put(data, "paired_count", paired.length());
            return ResultJson.success(data);
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.BLUETOOTH_CONNECT, "bluetooth_status");
        }
    }

    static JSONObject screenTap(JSONObject params) throws InterruptedException {
        float x = (float) params.optDouble("x", -1);
        float y = (float) params.optDouble("y", -1);
        if (x < 0 || y < 0) {
            return ResultJson.error("x and y required");
        }
        AadsAccessibilityService.GestureOutcome outcome = AadsAccessibilityService.tap(x, y);
        return gestureResult(outcome, "tap");
    }

    static JSONObject screenSwipe(JSONObject params) throws InterruptedException {
        float x1 = (float) params.optDouble("x1", -1);
        float y1 = (float) params.optDouble("y1", -1);
        float x2 = (float) params.optDouble("x2", -1);
        float y2 = (float) params.optDouble("y2", -1);
        if (x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) {
            return ResultJson.error("x1, y1, x2 and y2 required");
        }
        long durationMs = Math.max(50, Math.min(params.optLong("duration", params.optLong("duration_ms", 400)), 10000));
        AadsAccessibilityService.GestureOutcome outcome = AadsAccessibilityService.swipe(x1, y1, x2, y2, durationMs);
        return gestureResult(outcome, "swipe");
    }

    static JSONObject screenLongPress(JSONObject params) throws InterruptedException {
        float x = (float) params.optDouble("x", -1);
        float y = (float) params.optDouble("y", -1);
        if (x < 0 || y < 0) {
            return ResultJson.error("x and y required");
        }
        long durationMs = Math.max(300, Math.min(params.optLong("duration_ms", 1000), 10000));
        AadsAccessibilityService.GestureOutcome outcome = AadsAccessibilityService.longPress(x, y, durationMs);
        return gestureResult(outcome, "long_press");
    }

    static JSONObject screenText(JSONObject params) {
        AadsAccessibilityService.ScreenTextResult result = AadsAccessibilityService.getScreenText(
                boundedInt(params, "max_nodes", 200, 1, 1000)
        );
        if (!result.available) {
            return ResultJson.error(result.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "text", result.text);
        ResultJson.put(data, "nodes", result.nodes);
        ResultJson.put(data, "count", result.nodes.length());
        return ResultJson.success(data);
    }

    static JSONObject keyInput(JSONObject params) {
        String text = params.optString("text", "");
        if (text.isEmpty()) {
            return ResultJson.error("text required");
        }
        AadsAccessibilityService.InputOutcome outcome = AadsAccessibilityService.inputText(text, params.optBoolean("append", true));
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "inserted", outcome.completed);
        ResultJson.put(data, "length", text.length());
        return outcome.completed ? ResultJson.success(data) : ResultJson.error("input failed");
    }

    static JSONObject globalAction(JSONObject params) {
        String action = params.optString("action", params.optString("name", "")).trim();
        if (action.isEmpty()) {
            return ResultJson.error("action required");
        }
        AadsAccessibilityService.GlobalActionOutcome outcome = AadsAccessibilityService.performGlobalAction(action);
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "action", outcome.actionName);
        ResultJson.put(data, "action_code", outcome.actionCode);
        ResultJson.put(data, "performed", outcome.completed);
        return outcome.completed ? ResultJson.success(data) : ResultJson.error("global action failed");
    }

    static JSONObject findAndClick(JSONObject params) {
        String text = params.optString("text", "").trim();
        String id = params.optString("id", params.optString("view_id", "")).trim();
        if (text.isEmpty() && id.isEmpty()) {
            return ResultJson.error("text or id required");
        }
        AadsAccessibilityService.ClickOutcome outcome = text.isEmpty()
                ? AadsAccessibilityService.findAndClickByViewId(id)
                : AadsAccessibilityService.findAndClickByText(text);
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "clicked", outcome.completed);
        ResultJson.put(data, "match_text", outcome.text);
        ResultJson.put(data, "view_id", outcome.viewId);
        return outcome.completed ? ResultJson.success(data) : ResultJson.error("click failed");
    }

    static JSONObject screenScroll(JSONObject params) throws InterruptedException {
        String direction = params.optString("direction", "down").trim();
        AadsAccessibilityService.ScrollOutcome outcome = AadsAccessibilityService.scroll(direction);
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "direction", outcome.direction);
        ResultJson.put(data, "performed", outcome.completed);
        ResultJson.put(data, "method", outcome.method);
        return outcome.completed ? ResultJson.success(data) : ResultJson.error("scroll failed");
    }

    static JSONObject sensorData(Context context) throws InterruptedException {
        SensorManager manager = (SensorManager) context.getSystemService(Context.SENSOR_SERVICE);
        if (manager == null) {
            return ResultJson.error("sensor manager unavailable");
        }
        int timeoutMs = 1200;
        int[] sensorTypes = new int[]{
                Sensor.TYPE_ACCELEROMETER,
                Sensor.TYPE_GYROSCOPE,
                Sensor.TYPE_LIGHT
        };
        List<Sensor> sensors = new ArrayList<>();
        for (int type : sensorTypes) {
            Sensor sensor = manager.getDefaultSensor(type);
            if (sensor != null) {
                sensors.add(sensor);
            }
        }
        if (sensors.isEmpty()) {
            return ResultJson.error("requested sensors unavailable");
        }

        HandlerThread thread = new HandlerThread("AadsSensorRead");
        thread.start();
        CountDownLatch latch = new CountDownLatch(sensors.size());
        Map<Integer, SensorSnapshot> snapshots = Collections.synchronizedMap(new LinkedHashMap<>());
        Set<Integer> seenTypes = Collections.synchronizedSet(new HashSet<>());
        SensorEventListener listener = new SensorEventListener() {
            @Override
            public void onSensorChanged(SensorEvent event) {
                snapshots.put(event.sensor.getType(), new SensorSnapshot(event.sensor, event.values.clone(), event.timestamp, event.accuracy));
                if (seenTypes.add(event.sensor.getType())) {
                    latch.countDown();
                }
            }

            @Override
            public void onAccuracyChanged(Sensor sensor, int accuracy) {
            }
        };
        try {
            Handler handler = new Handler(thread.getLooper());
            for (Sensor sensor : sensors) {
                manager.registerListener(listener, sensor, SensorManager.SENSOR_DELAY_NORMAL, handler);
            }
            latch.await(timeoutMs, TimeUnit.MILLISECONDS);
        } finally {
            manager.unregisterListener(listener);
            thread.quitSafely();
        }
        if (snapshots.isEmpty()) {
            return ResultJson.timeout("sensor data timed out");
        }
        JSONArray array = new JSONArray();
        for (Sensor sensor : sensors) {
            SensorSnapshot snapshot = snapshots.get(sensor.getType());
            if (snapshot != null) {
                array.put(snapshot.toJson());
            }
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "sensors", array);
        ResultJson.put(data, "count", array.length());
        return ResultJson.success(data);
    }

    static JSONObject notificationRead(Context context, JSONObject params) {
        int limit = boundedInt(params, "limit", 50, 1, 200);
        JSONArray notifications = AadsNotificationListener.recentNotifications(limit);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "enabled", AadsNotificationListener.isEnabled(context));
        ResultJson.put(data, "notifications", notifications);
        ResultJson.put(data, "count", notifications.length());
        ResultJson.put(data, "limit", limit);
        return ResultJson.success(data);
    }

    static JSONObject screenshot(Context context, JSONObject params) throws InterruptedException {
        AadsAccessibilityService.ScreenshotOutcome outcome = AadsAccessibilityService.takeScreenshotBase64(
                Math.max(2000, Math.min(params.optInt("timeout_ms", 8000), 30000))
        );
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "mime", "image/png");
        ResultJson.put(data, "width", outcome.width);
        ResultJson.put(data, "height", outcome.height);
        ResultJson.put(data, "bytes", outcome.bytes);
        ResultJson.put(data, "base64", outcome.base64);
        return ResultJson.success(data);
    }

    static JSONObject deviceLock(Context context, JSONObject params) {
        DevicePolicyManager manager = (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
        ComponentName admin = AadsDeviceAdminReceiver.componentName(context);
        if (manager == null) {
            return ResultJson.error("device policy manager unavailable");
        }
        if (!manager.isAdminActive(admin)) {
            return ResultJson.error("device admin not active");
        }
        manager.lockNow();
        JSONObject data = new JSONObject();
        ResultJson.put(data, "locked", true);
        ResultJson.put(data, "user_visible_state", "device_locked");
        return ResultJson.success(data);
    }

    static JSONObject deviceWipe(Context context, JSONObject params) {
        if (!"WIPE_CONFIRMED".equals(params.optString("confirm", ""))) {
            return ResultJson.error("confirm must be WIPE_CONFIRMED");
        }
        DevicePolicyManager manager = (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
        ComponentName admin = AadsDeviceAdminReceiver.componentName(context);
        if (manager == null) {
            return ResultJson.error("device policy manager unavailable");
        }
        if (!manager.isAdminActive(admin)) {
            return ResultJson.error("device admin not active");
        }
        manager.wipeData(0);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "wipe_requested", true);
        return ResultJson.success(data);
    }

    static JSONObject deviceAdminStatus(Context context) {
        DevicePolicyManager manager = (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
        ComponentName admin = AadsDeviceAdminReceiver.componentName(context);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "active", manager != null && manager.isAdminActive(admin));
        ResultJson.put(data, "component", admin.flattenToString());
        return ResultJson.success(data);
    }

    static JSONObject screenBrightness(Context context, JSONObject params) {
        ContentResolver resolver = context.getContentResolver();
        try {
            if (params.has("brightness") || params.has("value")) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.System.canWrite(context)) {
                    return ResultJson.permissionError(Manifest.permission.WRITE_SETTINGS, "screen_brightness");
                }
                int value = Math.max(0, Math.min(params.optInt("brightness", params.optInt("value", 125)), 255));
                Settings.System.putInt(resolver, Settings.System.SCREEN_BRIGHTNESS, value);
            }
            JSONObject data = new JSONObject();
            ResultJson.put(data, "brightness", Settings.System.getInt(resolver, Settings.System.SCREEN_BRIGHTNESS));
            ResultJson.put(data, "mode", Settings.System.getInt(resolver, Settings.System.SCREEN_BRIGHTNESS_MODE, -1));
            ResultJson.put(data, "can_write", Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.System.canWrite(context));
            return ResultJson.success(data);
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.WRITE_SETTINGS, "screen_brightness");
        } catch (Settings.SettingNotFoundException e) {
            return ResultJson.error("screen brightness unavailable");
        }
    }

    static JSONObject screenTimeout(Context context, JSONObject params) {
        ContentResolver resolver = context.getContentResolver();
        try {
            if (params.has("timeout_ms") || params.has("value")) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.System.canWrite(context)) {
                    return ResultJson.permissionError(Manifest.permission.WRITE_SETTINGS, "screen_timeout");
                }
                int value = Math.max(5000, Math.min(params.optInt("timeout_ms", params.optInt("value", 30000)), 1800000));
                Settings.System.putInt(resolver, Settings.System.SCREEN_OFF_TIMEOUT, value);
            }
            JSONObject data = new JSONObject();
            ResultJson.put(data, "timeout_ms", Settings.System.getInt(resolver, Settings.System.SCREEN_OFF_TIMEOUT));
            ResultJson.put(data, "can_write", Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.System.canWrite(context));
            return ResultJson.success(data);
        } catch (SecurityException e) {
            return ResultJson.permissionError(Manifest.permission.WRITE_SETTINGS, "screen_timeout");
        } catch (Settings.SettingNotFoundException e) {
            return ResultJson.error("screen timeout unavailable");
        }
    }

    static JSONObject audioRecord(Context context, JSONObject params) throws Exception {
        if (!PermissionGate.has(context, Manifest.permission.RECORD_AUDIO)) {
            return ResultJson.permissionError(Manifest.permission.RECORD_AUDIO, "audio_record");
        }
        int durationSec = boundedInt(params, "duration_sec", 5, 1, 60);
        File file = File.createTempFile("aads-audio-", ".m4a", context.getCacheDir());
        MediaRecorder recorder = new MediaRecorder();
        try {
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC);
            recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            recorder.setAudioEncodingBitRate(96000);
            recorder.setAudioSamplingRate(44100);
            recorder.setOutputFile(file.getAbsolutePath());
            recorder.prepare();
            recorder.start();
            Thread.sleep(durationSec * 1000L);
            recorder.stop();
            byte[] bytes = readFileBytes(file);
            JSONObject data = new JSONObject();
            ResultJson.put(data, "mime", "audio/mp4");
            ResultJson.put(data, "duration_sec", durationSec);
            ResultJson.put(data, "bytes", bytes.length);
            ResultJson.put(data, "base64", Base64.encodeToString(bytes, Base64.NO_WRAP));
            return ResultJson.success(data);
        } finally {
            try {
                recorder.release();
            } catch (Exception ignored) {
            }
            if (!file.delete()) {
                file.deleteOnExit();
            }
        }
    }

    static JSONObject voiceWakeStart(Context context) {
        if (!PermissionGate.has(context, Manifest.permission.RECORD_AUDIO)) {
            return ResultJson.permissionError(Manifest.permission.RECORD_AUDIO, "voice_wake_start");
        }
        Intent intent = new Intent(context, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_VOICE_WAKE_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(intent);
        } else {
            context.startService(intent);
        }
        JSONObject data = voiceWakeStateJson(context);
        ResultJson.put(data, "requested", "start");
        return ResultJson.success(data);
    }

    static JSONObject voiceWakeStop(Context context) {
        Intent intent = new Intent(context, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_VOICE_WAKE_STOP);
        context.startService(intent);
        JSONObject data = voiceWakeStateJson(context);
        ResultJson.put(data, "requested", "stop");
        return ResultJson.success(data);
    }

    static JSONObject voiceWakeStatus(Context context) {
        return ResultJson.success(voiceWakeStateJson(context));
    }

    private static int boundedInt(JSONObject params, String key, int defaultValue, int min, int max) {
        return Math.max(min, Math.min(params.optInt(key, defaultValue), max));
    }

    private static JSONObject voiceWakeStateJson(Context context) {
        VoiceWakeState state = VoiceWakeController.loadState(context);
        JSONObject data = new JSONObject();
        ResultJson.put(data, "enabled", state.enabled);
        ResultJson.put(data, "status", state.status);
        ResultJson.put(data, "last_error", state.lastError);
        ResultJson.put(data, "last_text", state.lastText);
        ResultJson.put(data, "last_wake_ms", state.lastWakeMs);
        ResultJson.put(data, "deep_links", new JSONArray()
                .put("ohvis://wake")
                .put("aads-agent://wake"));
        ResultJson.put(data, "bixby_quick_command", "Open OHVIS with ohvis://wake");
        return data;
    }

    private static void addPermissionStatus(JSONArray array, Context context, String permission, String label, String commandType) {
        JSONObject item = new JSONObject();
        ResultJson.put(item, "label", label);
        ResultJson.put(item, "permission", permission);
        ResultJson.put(item, "command_type", commandType);
        ResultJson.put(item, "granted", PermissionGate.has(context, permission));
        array.put(item);
    }

    private static boolean allRuntimeGranted(JSONArray runtime) {
        for (int i = 0; i < runtime.length(); i++) {
            JSONObject item = runtime.optJSONObject(i);
            if (item != null && !item.optBoolean("granted", false)) {
                return false;
            }
        }
        return true;
    }

    private static boolean allSpecialReady(JSONObject special) {
        return special.optBoolean("accessibility_enabled", false)
                && special.optBoolean("accessibility_service_running", false)
                && special.optBoolean("notification_listener_enabled", false)
                && special.optBoolean("device_admin_active", false)
                && special.optBoolean("write_settings_allowed", false)
                && special.optBoolean("battery_optimization_ignored", false);
    }

    private static boolean isAccessibilityEnabled(Context context) {
        String enabled = Settings.Secure.getString(context.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (enabled == null) {
            return false;
        }
        String target = new ComponentName(context, AadsAccessibilityService.class).flattenToString().toLowerCase(Locale.US);
        return enabled.toLowerCase(Locale.US).contains(target);
    }

    private static boolean isBatteryOptimizationIgnored(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return true;
        }
        PowerManager manager = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        return manager != null && manager.isIgnoringBatteryOptimizations(context.getPackageName());
    }

    private static void appendContacts(Cursor cursor, JSONArray contacts, int limit, int offset) {
        if (cursor == null) {
            return;
        }
        int skipped = 0;
        while (cursor.moveToNext() && contacts.length() < limit) {
            if (skipped < offset) {
                skipped++;
                continue;
            }
            JSONObject item = new JSONObject();
            ResultJson.put(item, "id", cursor.getLong(cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.CONTACT_ID)));
            ResultJson.put(item, "name", cursor.getString(cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)));
            ResultJson.put(item, "phone", cursor.getString(cursor.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.NUMBER)));
            contacts.put(item);
        }
    }

    private static String callTypeName(int type) {
        switch (type) {
            case CallLog.Calls.INCOMING_TYPE:
                return "incoming";
            case CallLog.Calls.OUTGOING_TYPE:
                return "outgoing";
            case CallLog.Calls.MISSED_TYPE:
                return "missed";
            case CallLog.Calls.VOICEMAIL_TYPE:
                return "voicemail";
            case CallLog.Calls.REJECTED_TYPE:
                return "rejected";
            case CallLog.Calls.BLOCKED_TYPE:
                return "blocked";
            case CallLog.Calls.ANSWERED_EXTERNALLY_TYPE:
                return "answered_externally";
            default:
                return "unknown";
        }
    }

    private static String bluetoothStateName(int state) {
        switch (state) {
            case BluetoothAdapter.STATE_OFF:
                return "off";
            case BluetoothAdapter.STATE_TURNING_ON:
                return "turning_on";
            case BluetoothAdapter.STATE_ON:
                return "on";
            case BluetoothAdapter.STATE_TURNING_OFF:
                return "turning_off";
            default:
                return "unknown";
        }
    }

    private static JSONObject gestureResult(AadsAccessibilityService.GestureOutcome outcome, String action) {
        if (!outcome.available) {
            return ResultJson.error(outcome.error);
        }
        JSONObject data = new JSONObject();
        ResultJson.put(data, "action", action);
        ResultJson.put(data, "accepted", outcome.accepted);
        ResultJson.put(data, "completed", outcome.completed);
        return outcome.completed ? ResultJson.success(data) : ResultJson.error("gesture failed");
    }

    private static byte[] readFileBytes(File file) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (FileInputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) != -1) {
                output.write(buffer, 0, read);
            }
        }
        return output.toByteArray();
    }

    private static final class SensorSnapshot {
        private final Sensor sensor;
        private final float[] values;
        private final long timestamp;
        private final int accuracy;

        private SensorSnapshot(Sensor sensor, float[] values, long timestamp, int accuracy) {
            this.sensor = sensor;
            this.values = values;
            this.timestamp = timestamp;
            this.accuracy = accuracy;
        }

        private JSONObject toJson() {
            JSONArray jsonValues = new JSONArray();
            for (float value : values) {
                try {
                    jsonValues.put(value);
                } catch (org.json.JSONException ignored) {
                    // Skip non-finite values (NaN/Infinity)
                }
            }
            JSONObject data = new JSONObject();
            ResultJson.put(data, "type", sensor.getType());
            ResultJson.put(data, "type_name", sensorTypeName(sensor.getType()));
            ResultJson.put(data, "name", sensor.getName());
            ResultJson.put(data, "values", jsonValues);
            ResultJson.put(data, "timestamp_ns", timestamp);
            ResultJson.put(data, "accuracy", accuracy);
            return data;
        }
    }

    private static String sensorTypeName(int type) {
        switch (type) {
            case Sensor.TYPE_ACCELEROMETER:
                return "accelerometer";
            case Sensor.TYPE_GYROSCOPE:
                return "gyroscope";
            case Sensor.TYPE_LIGHT:
                return "light";
            default:
                return "unknown";
        }
    }

    private static Location bestLastKnownLocation(LocationManager manager) {
        Location best = null;
        for (String provider : manager.getProviders(true)) {
            try {
                Location candidate = manager.getLastKnownLocation(provider);
                if (candidate != null && (best == null || candidate.getTime() > best.getTime())) {
                    best = candidate;
                }
            } catch (SecurityException ignored) {
            }
        }
        return best;
    }

    private static JSONObject locationToJson(Location location) {
        JSONObject data = new JSONObject();
        ResultJson.put(data, "provider", location.getProvider());
        ResultJson.put(data, "latitude", location.getLatitude());
        ResultJson.put(data, "longitude", location.getLongitude());
        ResultJson.put(data, "accuracy_m", location.hasAccuracy() ? location.getAccuracy() : JSONObject.NULL);
        ResultJson.put(data, "altitude_m", location.hasAltitude() ? location.getAltitude() : JSONObject.NULL);
        ResultJson.put(data, "bearing", location.hasBearing() ? location.getBearing() : JSONObject.NULL);
        ResultJson.put(data, "speed_mps", location.hasSpeed() ? location.getSpeed() : JSONObject.NULL);
        ResultJson.put(data, "time_ms", location.getTime());
        return data;
    }

    private static String findBackCamera(CameraManager manager) throws CameraAccessException {
        String first = "";
        for (String id : manager.getCameraIdList()) {
            if (first.isEmpty()) {
                first = id;
            }
            CameraCharacteristics characteristics = manager.getCameraCharacteristics(id);
            Integer facing = characteristics.get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) {
                return id;
            }
        }
        return first;
    }

    private static Size chooseCaptureSize(CameraCharacteristics characteristics, int maxWidth, int maxHeight) {
        StreamConfigurationMap map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) {
            return new Size(Math.max(320, maxWidth), Math.max(240, maxHeight));
        }
        Size[] sizes = map.getOutputSizes(android.graphics.ImageFormat.JPEG);
        if (sizes == null || sizes.length == 0) {
            return new Size(640, 480);
        }
        Arrays.sort(sizes, Comparator.comparingInt(size -> size.getWidth() * size.getHeight()));
        Size fallback = sizes[0];
        for (Size size : sizes) {
            if (size.getWidth() <= maxWidth && size.getHeight() <= maxHeight) {
                fallback = size;
            }
        }
        return fallback;
    }

    private static TextToSpeech getTextToSpeech(Context context) throws InterruptedException {
        synchronized (TTS_LOCK) {
            if (textToSpeech != null && textToSpeechReady) {
                return textToSpeech;
            }
            CountDownLatch latch = new CountDownLatch(1);
            textToSpeech = new TextToSpeech(context.getApplicationContext(), status -> {
                textToSpeechReady = status == TextToSpeech.SUCCESS;
                latch.countDown();
            });
            latch.await(4, TimeUnit.SECONDS);
            return textToSpeech;
        }
    }

    private static int streamType(String name) {
        switch (name) {
            case "alarm":
                return AudioManager.STREAM_ALARM;
            case "ring":
                return AudioManager.STREAM_RING;
            case "notification":
                return AudioManager.STREAM_NOTIFICATION;
            case "system":
                return AudioManager.STREAM_SYSTEM;
            case "voice_call":
                return AudioManager.STREAM_VOICE_CALL;
            case "music":
            default:
                return AudioManager.STREAM_MUSIC;
        }
    }

    private static String stripQuotes(String value) {
        if (value == null) {
            return "";
        }
        if (value.length() >= 2 && value.startsWith("\"") && value.endsWith("\"")) {
            return value.substring(1, value.length() - 1);
        }
        return value;
    }

    private static List<String> tokenizeCommand(String command) {
        if (command.contains(";") || command.contains("|") || command.contains("&")
                || command.contains("`") || command.contains("$") || command.contains(">")
                || command.contains("<") || command.contains("\n") || command.contains("\r")) {
            return Collections.emptyList();
        }
        List<String> tokens = new ArrayList<>();
        Matcher matcher = TOKEN_PATTERN.matcher(command);
        while (matcher.find()) {
            String token = matcher.group(1);
            if (token == null) {
                token = matcher.group(2);
            }
            if (token == null) {
                token = matcher.group();
            }
            tokens.add(token);
        }
        return tokens;
    }

    private static boolean isAllowedShellCommand(List<String> tokens) {
        if (tokens.isEmpty()) {
            return false;
        }
        for (String token : tokens) {
            if (!SAFE_ARG_PATTERN.matcher(token).matches()) {
                return false;
            }
        }
        String cmd = tokens.get(0);
        if ("getprop".equals(cmd)) {
            return tokens.size() <= 2;
        }
        if ("settings".equals(cmd)) {
            return tokens.size() == 4
                    && "get".equals(tokens.get(1))
                    && ("secure".equals(tokens.get(2)) || "system".equals(tokens.get(2)) || "global".equals(tokens.get(2)));
        }
        if ("dumpsys".equals(cmd)) {
            return tokens.size() == 2
                    && ("battery".equals(tokens.get(1)) || "wifi".equals(tokens.get(1))
                    || "connectivity".equals(tokens.get(1)) || "power".equals(tokens.get(1)));
        }
        if ("pm".equals(cmd)) {
            return tokens.size() >= 3
                    && tokens.size() <= 4
                    && "list".equals(tokens.get(1))
                    && "packages".equals(tokens.get(2))
                    && (tokens.size() == 3 || "-3".equals(tokens.get(3)));
        }
        if ("id".equals(cmd)) {
            return tokens.size() == 1;
        }
        if ("uname".equals(cmd)) {
            return tokens.size() == 1 || (tokens.size() == 2 && "-a".equals(tokens.get(1)));
        }
        return false;
    }

    private static void copyCapped(InputStream inputStream, ByteArrayOutputStream output, int maxBytes) {
        byte[] buffer = new byte[1024];
        int total = 0;
        try {
            int read;
            while ((read = inputStream.read(buffer)) != -1) {
                if (total < maxBytes) {
                    int keep = Math.min(read, maxBytes - total);
                    output.write(buffer, 0, keep);
                    total += keep;
                }
            }
        } catch (Exception ignored) {
        }
    }
}
