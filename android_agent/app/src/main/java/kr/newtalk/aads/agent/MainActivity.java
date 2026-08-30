package kr.newtalk.aads.agent;

import android.Manifest;
import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public final class MainActivity extends Activity {
    static final String ACTION_WAKE_FROM_VOICE = "kr.newtalk.aads.agent.action.WAKE_FROM_VOICE";
    private static final String OHVIS_HOME_URL = "https://aads.newtalk.kr";
    private static final String OHVIS_CHAT_URL = OHVIS_HOME_URL + "/chat";

    private static final int REQ_NOTIFICATIONS = 10;
    private static final int REQ_LOCATION = 11;
    private static final int REQ_CAMERA = 12;
    private static final int REQ_SMS = 13;
    private static final int REQ_WIFI = 14;
    private static final int REQ_DATA = 15;
    private static final int REQ_MIC = 16;
    private static final int REQ_BLUETOOTH = 17;

    private EditText serverUrlEdit;
    private EditText tokenEdit;
    private EditText qrEdit;
    private TextView agentIdView;
    private TextView statusView;
    private TextView heartbeatView;
    private TextView activeCommandView;
    private TextView lastErrorView;
    private TextView voiceWakeView;
    private TextView ohvisWebStatusView;
    private WebView ohvisWebView;
    private LinearLayout agentSettingsPanel;

    private final BroadcastReceiver stateReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refreshState();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildContent());
        loadPairingFields();
        applyPairingIntent(getIntent());
        if (!applyWakeIntent(getIntent())) {
            openOhvisWeb(OHVIS_CHAT_URL, "launch");
        }
        refreshState();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        applyPairingIntent(intent);
        applyWakeIntent(intent);
        refreshState();
    }

    @Override
    protected void onResume() {
        super.onResume();
        IntentFilter filter = new IntentFilter(AadsForegroundService.ACTION_STATE_CHANGED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(stateReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(stateReceiver, filter);
        }
        refreshState();
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(stateReceiver);
        } catch (IllegalArgumentException ignored) {
        }
    }

    private View buildContent() {
        ScrollView scrollView = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(18), dp(18), dp(24));
        scrollView.addView(root);

        TextView title = text("오비스", 22, true);
        root.addView(title);

        statusView = text("", 16, true);
        heartbeatView = text("", 14, false);
        activeCommandView = text("", 14, false);
        lastErrorView = text("", 14, false);
        voiceWakeView = text("", 14, false);
        root.addView(statusView);
        root.addView(heartbeatView);
        root.addView(activeCommandView);
        root.addView(lastErrorView);
        root.addView(voiceWakeView);

        agentSettingsPanel = new LinearLayout(this);
        agentSettingsPanel.setOrientation(LinearLayout.VERTICAL);
        agentSettingsPanel.setVisibility(View.GONE);
        root.addView(section("Agent Settings"));
        root.addView(row(button("Show Settings", v -> showAgentSettings(true)), button("Hide Settings", v -> showAgentSettings(false))));

        agentSettingsPanel.addView(section("Pairing"));
        serverUrlEdit = edit("Server WebSocket URL", false);
        tokenEdit = edit("Pairing token", true);
        agentIdView = text("", 14, true);
        qrEdit = edit("Paste pairing JSON or full WebSocket URL", false);
        qrEdit.setMinLines(2);

        agentSettingsPanel.addView(label("Server URL"));
        agentSettingsPanel.addView(serverUrlEdit);
        agentSettingsPanel.addView(label("Agent ID"));
        agentSettingsPanel.addView(agentIdView);
        agentSettingsPanel.addView(row(button("Regenerate", this::regenerateAgentId), button("Save", this::savePairing)));
        agentSettingsPanel.addView(label("Token"));
        agentSettingsPanel.addView(tokenEdit);
        agentSettingsPanel.addView(label("QR input hook"));
        agentSettingsPanel.addView(qrEdit);
        agentSettingsPanel.addView(row(button("Apply Input", this::applyPairingInput), button("Clear Input", v -> qrEdit.setText(""))));

        agentSettingsPanel.addView(section("Service"));
        agentSettingsPanel.addView(row(button("Start", this::startAgentService), button("Stop", this::stopAgentService)));

        agentSettingsPanel.addView(section("Voice"));
        agentSettingsPanel.addView(row(button("Start Wake", this::startVoiceWake), button("Stop Wake", this::stopVoiceWake)));
        agentSettingsPanel.addView(row(button("Bixby Wake", this::openBixbyWakeLink), button("Mic Permission", v -> requestPermission(REQ_MIC, Manifest.permission.RECORD_AUDIO))));
        root.addView(agentSettingsPanel);

        root.addView(section("OHVIS"));
        ohvisWebStatusView = text("OHVIS Web: not opened", 14, false);
        root.addView(ohvisWebStatusView);
        root.addView(row(button("Open Chat", v -> openOhvisWeb(OHVIS_CHAT_URL, "button")), button("Refresh", v -> reloadOhvisWeb())));
        root.addView(row(button("Close OHVIS", v -> closeOhvisWeb()), button("Open in Browser", v -> openOhvisExternal())));
        ohvisWebView = new WebView(this);
        ohvisWebView.setVisibility(View.GONE);
        ohvisWebView.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(620)
        ));
        configureOhvisWebView();
        root.addView(ohvisWebView);

        agentSettingsPanel.addView(section("Permissions"));
        agentSettingsPanel.addView(row(button("Notifications", v -> requestNotificationPermission()), button("Location", v -> requestLocationPermission())));
        agentSettingsPanel.addView(row(button("Camera", v -> requestPermission(REQ_CAMERA, Manifest.permission.CAMERA)), button("SMS", v -> requestPermission(REQ_SMS, Manifest.permission.SEND_SMS))));
        agentSettingsPanel.addView(row(button("Wi-Fi", v -> requestWifiPermission()), button("Battery Settings", this::openBatterySettings)));
        agentSettingsPanel.addView(row(button("Data Access", v -> requestDataPermissions()), button("Microphone", v -> requestPermission(REQ_MIC, Manifest.permission.RECORD_AUDIO))));
        agentSettingsPanel.addView(row(button("Bluetooth", v -> requestBluetoothPermission()), button("Write Settings", this::openWriteSettings)));
        agentSettingsPanel.addView(row(button("Accessibility", this::openAccessibilitySettings), button("Notify Access", this::openNotificationAccessSettings)));
        agentSettingsPanel.addView(row(button("Device Admin", this::openDeviceAdminSettings), button("System Settings", v -> startActivity(new Intent(Settings.ACTION_SETTINGS)))));

        return scrollView;
    }

    private void loadPairingFields() {
        AgentConfig config = AgentPrefs.load(this);
        serverUrlEdit.setText(config.serverUrl);
        tokenEdit.setText(config.token);
        agentIdView.setText(config.agentId);
    }

    private void savePairing(View view) {
        AgentPrefs.save(this, serverUrlEdit.getText().toString(), agentIdView.getText().toString(), tokenEdit.getText().toString());
        toast("Pairing saved");
    }

    private void regenerateAgentId(View view) {
        agentIdView.setText(AgentPrefs.regenerateAgentId(this));
        savePairing(view);
    }

    private void applyPairingInput(View view) {
        AgentConfig fallback = new AgentConfig(
                serverUrlEdit.getText().toString(),
                agentIdView.getText().toString(),
                tokenEdit.getText().toString()
        );
        PairingData data = PairingParser.parse(qrEdit.getText().toString(), fallback);
        serverUrlEdit.setText(AgentPrefs.normalizeServerUrl(data.serverUrl));
        agentIdView.setText(data.agentId);
        tokenEdit.setText(data.token);
        savePairing(view);
    }

    private void applyPairingIntent(Intent intent) {
        if (intent == null || intent.getData() == null) {
            return;
        }
        Uri dataUri = intent.getData();
        String raw = dataUri.toString();
        AgentConfig fallback = new AgentConfig(
                serverUrlEdit.getText().toString(),
                agentIdView.getText().toString(),
                tokenEdit.getText().toString()
        );
        PairingData data = PairingParser.parse(raw, fallback);
        if (data.token == null || data.token.trim().isEmpty()) {
            return;
        }
        serverUrlEdit.setText(AgentPrefs.normalizeServerUrl(data.serverUrl));
        agentIdView.setText(data.agentId);
        tokenEdit.setText(data.token);
        savePairing(null);
        startAgentService(null);
        toast("Pairing applied");
    }

    private boolean applyWakeIntent(Intent intent) {
        if (intent == null) {
            return false;
        }
        Uri dataUri = intent.getData();
        boolean wakeAction = ACTION_WAKE_FROM_VOICE.equals(intent.getAction());
        boolean wakeLink = dataUri != null
                && ("ohvis".equalsIgnoreCase(dataUri.getScheme()) || "aads-agent".equalsIgnoreCase(dataUri.getScheme()))
                && ("wake".equalsIgnoreCase(dataUri.getHost()) || "open".equalsIgnoreCase(dataUri.getHost()));
        if (!wakeAction && !wakeLink) {
            return false;
        }
        startAgentService(null);
        if (PermissionGate.has(this, Manifest.permission.RECORD_AUDIO)) {
            startVoiceWake(null);
        }
        openOhvisWeb(resolveOhvisUrl(dataUri), wakeAction ? "voice" : "deeplink");
        toast("OHVIS wake");
        return true;
    }

    private void showAgentSettings(boolean show) {
        if (agentSettingsPanel != null) {
            agentSettingsPanel.setVisibility(show ? View.VISIBLE : View.GONE);
        }
    }

    private String resolveOhvisUrl(Uri dataUri) {
        if (dataUri == null) {
            return OHVIS_CHAT_URL;
        }
        String target = dataUri.getQueryParameter("target");
        if (target == null || target.trim().isEmpty()) {
            target = dataUri.getQueryParameter("url");
        }
        if (target == null || target.trim().isEmpty()) {
            return OHVIS_CHAT_URL;
        }
        String normalized = target.trim();
        if (normalized.startsWith("/")) {
            return OHVIS_HOME_URL + normalized;
        }
        if (normalized.startsWith(OHVIS_HOME_URL + "/") || OHVIS_HOME_URL.equals(normalized)) {
            return normalized;
        }
        return OHVIS_CHAT_URL;
    }

    private void startAgentService(View view) {
        savePairing(view);
        Intent intent = new Intent(this, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void stopAgentService(View view) {
        Intent intent = new Intent(this, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_STOP);
        startService(intent);
    }

    private void startVoiceWake(View view) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !PermissionGate.has(this, Manifest.permission.RECORD_AUDIO)) {
            requestPermission(REQ_MIC, Manifest.permission.RECORD_AUDIO);
            return;
        }
        savePairing(view);
        Intent intent = new Intent(this, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_VOICE_WAKE_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        toast("Voice wake enabled");
    }

    private void stopVoiceWake(View view) {
        Intent intent = new Intent(this, AadsForegroundService.class);
        intent.setAction(AadsForegroundService.ACTION_VOICE_WAKE_STOP);
        startService(intent);
        toast("Voice wake disabled");
    }

    private void openBixbyWakeLink(View view) {
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse("ohvis://wake?source=bixby&target=/chat"));
        intent.setPackage(getPackageName());
        startActivity(intent);
    }

    private void configureOhvisWebView() {
        if (ohvisWebView == null) {
            return;
        }
        WebSettings settings = ohvisWebView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(true);
        settings.setBuiltInZoomControls(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookieManager.setAcceptThirdPartyCookies(ohvisWebView, true);
        }
        ohvisWebView.setWebChromeClient(new WebChromeClient());
        ohvisWebView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request == null ? null : request.getUrl();
                if (uri == null) {
                    return false;
                }
                String url = uri.toString();
                if (url.startsWith(OHVIS_HOME_URL + "/") || OHVIS_HOME_URL.equals(url)) {
                    return false;
                }
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                ohvisWebStatusView.setText("OHVIS Web: loaded " + safeUrl(url));
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    String description = error == null ? "unknown" : String.valueOf(error.getDescription());
                    ohvisWebStatusView.setText("OHVIS Web error: " + description);
                }
            }
        });
    }

    private void openOhvisWeb(String url, String source) {
        if (ohvisWebView == null) {
            return;
        }
        String targetUrl = resolveOhvisUrl(Uri.parse("ohvis://open?target=" + Uri.encode(url == null ? "" : url)));
        ohvisWebView.setVisibility(View.VISIBLE);
        ohvisWebStatusView.setText("OHVIS Web: loading from " + source);
        startAgentService(null);
        ohvisWebView.loadUrl(targetUrl);
    }

    private void reloadOhvisWeb() {
        if (ohvisWebView == null) {
            return;
        }
        if (ohvisWebView.getUrl() == null || ohvisWebView.getUrl().trim().isEmpty()) {
            openOhvisWeb(OHVIS_CHAT_URL, "refresh");
            return;
        }
        ohvisWebStatusView.setText("OHVIS Web: reloading");
        ohvisWebView.reload();
    }

    private void closeOhvisWeb() {
        if (ohvisWebView == null) {
            return;
        }
        ohvisWebView.stopLoading();
        ohvisWebView.setVisibility(View.GONE);
        ohvisWebStatusView.setText("OHVIS Web: closed");
    }

    private void openOhvisExternal() {
        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(OHVIS_CHAT_URL)));
    }

    private void refreshState() {
        AgentStateSnapshot snapshot = AgentStateStore.load(this);
        VoiceWakeState voiceWake = VoiceWakeController.loadState(this);
        statusView.setText("Status: " + snapshot.status);
        heartbeatView.setText("Last heartbeat: " + formatHeartbeat(snapshot.lastHeartbeatMs));
        activeCommandView.setText("Visible command state: " + emptyToDash(snapshot.activeCommand));
        lastErrorView.setText("Last error: " + emptyToDash(snapshot.lastError));
        voiceWakeView.setText("Voice wake: " + (voiceWake.enabled ? voiceWake.status : "disabled")
                + " / last: " + emptyToDash(voiceWake.lastText)
                + " / error: " + emptyToDash(voiceWake.lastError));
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermission(REQ_NOTIFICATIONS, Manifest.permission.POST_NOTIFICATIONS);
        } else {
            toast("Notification permission is already available on this Android version");
        }
    }

    private void requestLocationPermission() {
        requestPermissions(new String[]{
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
        }, REQ_LOCATION);
    }

    private void requestWifiPermission() {
        List<String> permissions = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= 33) {
            permissions.add(Manifest.permission.NEARBY_WIFI_DEVICES);
        }
        permissions.add(Manifest.permission.ACCESS_FINE_LOCATION);
        requestPermissions(permissions.toArray(new String[0]), REQ_WIFI);
    }

    private void requestDataPermissions() {
        List<String> permissions = new ArrayList<>();
        permissions.add(Manifest.permission.READ_CONTACTS);
        permissions.add(Manifest.permission.READ_CALL_LOG);
        permissions.add(Manifest.permission.READ_SMS);
        if (Build.VERSION.SDK_INT >= 33) {
            permissions.add(Manifest.permission.READ_MEDIA_IMAGES);
        } else {
            permissions.add(Manifest.permission.READ_EXTERNAL_STORAGE);
        }
        requestPermissions(permissions.toArray(new String[0]), REQ_DATA);
    }

    private void requestBluetoothPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            requestPermission(REQ_BLUETOOTH, Manifest.permission.BLUETOOTH_CONNECT);
        } else {
            toast("Bluetooth permission is already available on this Android version");
        }
    }

    private void requestPermission(int requestCode, String permission) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !PermissionGate.has(this, permission)) {
            requestPermissions(new String[]{permission}, requestCode);
        } else {
            toast("Permission already granted");
        }
    }

    private void openBatterySettings(View view) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
                if (powerManager != null && !powerManager.isIgnoringBatteryOptimizations(getPackageName())) {
                    Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                    return;
                }
            }
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_SETTINGS));
        }
    }

    private void openWriteSettings(View view) {
        try {
            Intent intent = new Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS);
            intent.setData(Uri.parse("package:" + getPackageName()));
            startActivity(intent);
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_SETTINGS));
        }
    }

    private void openAccessibilitySettings(View view) {
        startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
    }

    private void openNotificationAccessSettings(View view) {
        startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
    }

    private void openDeviceAdminSettings(View view) {
        try {
            Intent intent = new Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN);
            intent.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, AadsDeviceAdminReceiver.componentName(this));
            intent.putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION, "Enable AADS device lock and wipe control");
            startActivity(intent);
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
        }
    }

    private TextView section(String text) {
        TextView view = text(text, 18, true);
        view.setPadding(0, dp(22), 0, dp(6));
        return view;
    }

    private TextView label(String text) {
        TextView view = text(text, 13, false);
        view.setPadding(0, dp(10), 0, dp(4));
        return view;
    }

    private TextView text(String value, int sp, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        if (bold) {
            view.setTypeface(view.getTypeface(), android.graphics.Typeface.BOLD);
        }
        view.setPadding(0, dp(4), 0, dp(4));
        return view;
    }

    private EditText edit(String hint, boolean secret) {
        EditText editText = new EditText(this);
        editText.setHint(hint);
        editText.setSingleLine(!hint.startsWith("Paste"));
        editText.setInputType(secret
                ? InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        editText.setLayoutParams(matchWrap());
        return editText;
    }

    private Button button(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, dp(48), 1f);
        params.setMargins(dp(3), dp(3), dp(3), dp(3));
        button.setLayoutParams(params);
        return button;
    }

    private LinearLayout row(View first, View second) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.addView(first);
        row.addView(second);
        row.setLayoutParams(matchWrap());
        return row;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private String formatHeartbeat(long timestampMs) {
        if (timestampMs <= 0L) {
            return "never";
        }
        return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date(timestampMs));
    }

    private String emptyToDash(String value) {
        return value == null || value.trim().isEmpty() ? "-" : value;
    }

    private String safeUrl(String value) {
        if (value == null || value.trim().isEmpty()) {
            return "-";
        }
        if (value.startsWith(OHVIS_HOME_URL)) {
            return value.substring(OHVIS_HOME_URL.length()).isEmpty()
                    ? "/"
                    : value.substring(OHVIS_HOME_URL.length());
        }
        return "external";
    }

    @Override
    public void onBackPressed() {
        if (ohvisWebView != null && ohvisWebView.getVisibility() == View.VISIBLE) {
            if (ohvisWebView.canGoBack()) {
                ohvisWebView.goBack();
                return;
            }
            closeOhvisWeb();
            return;
        }
        super.onBackPressed();
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }
}
