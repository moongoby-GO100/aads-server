package kr.newtalk.aads.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public final class AadsBootReceiver extends BroadcastReceiver {
    private static final String TAG = "AadsBootReceiver";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !"android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            return;
        }
        AgentConfig config = AgentPrefs.load(context);
        if (!config.isPairingReady()) {
            Log.i(TAG, "Pairing not ready, skip auto-start");
            return;
        }
        Log.i(TAG, "Boot completed — starting AADS Agent service");
        Intent serviceIntent = new Intent(context, AadsForegroundService.class);
        serviceIntent.setAction(AadsForegroundService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(serviceIntent);
        } else {
            context.startService(serviceIntent);
        }
    }
}
