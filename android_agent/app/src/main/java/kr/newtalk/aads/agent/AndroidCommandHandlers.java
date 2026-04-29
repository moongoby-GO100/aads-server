package kr.newtalk.aads.agent;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
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
import android.net.Uri;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.speech.tts.TextToSpeech;
import android.telephony.SmsManager;
import android.util.Base64;
import android.util.Size;
import android.view.Surface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
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
