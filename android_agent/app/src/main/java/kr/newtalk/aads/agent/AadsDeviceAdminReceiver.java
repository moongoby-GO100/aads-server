package kr.newtalk.aads.agent;

import android.app.admin.DeviceAdminReceiver;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;

public final class AadsDeviceAdminReceiver extends DeviceAdminReceiver {
    static ComponentName componentName(Context context) {
        return new ComponentName(context.getApplicationContext(), AadsDeviceAdminReceiver.class);
    }

    static boolean isAdminActive(Context context) {
        DevicePolicyManager manager = (DevicePolicyManager) context.getSystemService(Context.DEVICE_POLICY_SERVICE);
        return manager != null && manager.isAdminActive(componentName(context));
    }
}
