package kr.newtalk.aads.agent;

import android.Manifest;
import android.app.Activity;
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
    private static final int REQ_NOTIFICATIONS = 10;
    private static final int REQ_LOCATION = 11;
    private static final int REQ_CAMERA = 12;
    private static final int REQ_SMS = 13;
    private static final int REQ_WIFI = 14;

    private EditText serverUrlEdit;
    private EditText tokenEdit;
    private EditText qrEdit;
    private TextView agentIdView;
    private TextView statusView;
    private TextView heartbeatView;
    private TextView activeCommandView;
    private TextView lastErrorView;

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

        TextView title = text("AADS Android Agent", 22, true);
        root.addView(title);

        statusView = text("", 16, true);
        heartbeatView = text("", 14, false);
        activeCommandView = text("", 14, false);
        lastErrorView = text("", 14, false);
        root.addView(statusView);
        root.addView(heartbeatView);
        root.addView(activeCommandView);
        root.addView(lastErrorView);

        root.addView(section("Pairing"));
        serverUrlEdit = edit("Server WebSocket URL", false);
        tokenEdit = edit("Pairing token", true);
        agentIdView = text("", 14, true);
        qrEdit = edit("Paste pairing JSON or full WebSocket URL", false);
        qrEdit.setMinLines(2);

        root.addView(label("Server URL"));
        root.addView(serverUrlEdit);
        root.addView(label("Agent ID"));
        root.addView(agentIdView);
        root.addView(row(button("Regenerate", this::regenerateAgentId), button("Save", this::savePairing)));
        root.addView(label("Token"));
        root.addView(tokenEdit);
        root.addView(label("QR input hook"));
        root.addView(qrEdit);
        root.addView(row(button("Apply Input", this::applyPairingInput), button("Clear Input", v -> qrEdit.setText(""))));

        root.addView(section("Service"));
        root.addView(row(button("Start", this::startAgentService), button("Stop", this::stopAgentService)));

        root.addView(section("Permissions"));
        root.addView(row(button("Notifications", v -> requestNotificationPermission()), button("Location", v -> requestLocationPermission())));
        root.addView(row(button("Camera", v -> requestPermission(REQ_CAMERA, Manifest.permission.CAMERA)), button("SMS", v -> requestPermission(REQ_SMS, Manifest.permission.SEND_SMS))));
        root.addView(row(button("Wi-Fi", v -> requestWifiPermission()), button("Battery Settings", this::openBatterySettings)));

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

    private void refreshState() {
        AgentStateSnapshot snapshot = AgentStateStore.load(this);
        statusView.setText("Status: " + snapshot.status);
        heartbeatView.setText("Last heartbeat: " + formatHeartbeat(snapshot.lastHeartbeatMs));
        activeCommandView.setText("Visible command state: " + emptyToDash(snapshot.activeCommand));
        lastErrorView.setText("Last error: " + emptyToDash(snapshot.lastError));
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

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }
}
