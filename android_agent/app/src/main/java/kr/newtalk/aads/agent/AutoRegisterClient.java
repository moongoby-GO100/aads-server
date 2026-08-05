package kr.newtalk.aads.agent;

import android.content.Context;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

final class AutoRegisterClient {
    private static final String TAG = "AutoRegister";

    private AutoRegisterClient() {
    }

    static PairingData register(Context context) throws Exception {
        String deviceId = Settings.Secure.getString(
                context.getContentResolver(), Settings.Secure.ANDROID_ID);
        if (deviceId == null || deviceId.trim().isEmpty()) {
            deviceId = "fallback-" + Integer.toHexString(
                    (Build.FINGERPRINT + ":" + Build.MANUFACTURER + ":" + Build.MODEL).hashCode());
        }
        String deviceName = Build.MANUFACTURER + " " + Build.MODEL;
        String url = buildAutoRegisterUrl(AgentConfig.DEFAULT_SERVER_URL);

        Log.i(TAG, "auto-register URL: " + url);
        Log.i(TAG, "device_id: " + deviceId + ", device_name: " + deviceName);

        JSONObject body = new JSONObject();
        body.put("device_id", deviceId);
        body.put("device_name", deviceName);

        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod("POST");
        conn.setRequestProperty("Content-Type", "application/json");
        conn.setRequestProperty("Accept", "application/json");
        conn.setRequestProperty("User-Agent", "AADS-Android-Agent/" + AgentConfig.VERSION);
        conn.setDoOutput(true);
        conn.setConnectTimeout(15_000);
        conn.setReadTimeout(15_000);

        try (OutputStream os = conn.getOutputStream()) {
            os.write(body.toString().getBytes("UTF-8"));
        }

        int code = conn.getResponseCode();
        Log.i(TAG, "HTTP response code: " + code);

        if (code != 200) {
            StringBuilder errBody = new StringBuilder();
            try (BufferedReader br = new BufferedReader(
                    new InputStreamReader(conn.getErrorStream(), "UTF-8"))) {
                String line;
                while ((line = br.readLine()) != null) {
                    errBody.append(line);
                }
            } catch (Exception ignored) {
            }
            throw new Exception("HTTP " + code + ": " + errBody);
        }

        StringBuilder sb = new StringBuilder();
        try (BufferedReader br = new BufferedReader(
                new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
            String line;
            while ((line = br.readLine()) != null) {
                sb.append(line);
            }
        }

        Log.i(TAG, "auto-register success");
        JSONObject resp = new JSONObject(sb.toString());
        return new PairingData(
                resp.getString("server_url"),
                resp.getString("agent_id"),
                resp.getString("token")
        );
    }

    static String buildAutoRegisterUrl(String wsServerUrl) {
        String url = wsServerUrl;
        if (url.startsWith("wss://")) {
            url = "https://" + url.substring(6);
        } else if (url.startsWith("ws://")) {
            url = "http://" + url.substring(5);
        }
        if (url.endsWith("/ws")) {
            url = url.substring(0, url.length() - 3);
        }
        return url + "/android/auto-register";
    }
}
