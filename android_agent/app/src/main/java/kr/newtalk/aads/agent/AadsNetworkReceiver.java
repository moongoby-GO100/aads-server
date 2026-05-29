package kr.newtalk.aads.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.util.Log;

public final class AadsNetworkReceiver extends BroadcastReceiver {
    private static final String TAG = "AadsNetworkReceiver";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        ConnectivityManager cm =
                (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null) return;
        NetworkInfo info = cm.getActiveNetworkInfo();
        if (info != null && info.isConnected()) {
            Log.i(TAG, "Network restored — nudging reconnect");
            Intent nudge = new Intent(context, AadsForegroundService.class);
            nudge.setAction(AadsForegroundService.ACTION_NETWORK_RESTORED);
            context.startService(nudge);
        }
    }
}
