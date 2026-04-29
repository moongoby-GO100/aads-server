package kr.newtalk.aads.agent;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;

final class PermissionGate {
    private PermissionGate() {
    }

    static boolean has(Context context, String permission) {
        if (permission == null || permission.trim().isEmpty()) {
            return true;
        }
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M
                || context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    static boolean hasAnyLocation(Context context) {
        return has(context, Manifest.permission.ACCESS_FINE_LOCATION)
                || has(context, Manifest.permission.ACCESS_COARSE_LOCATION);
    }

    static boolean hasNotification(Context context) {
        return Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.POST_NOTIFICATIONS);
    }

    static boolean hasNearbyWifi(Context context) {
        return Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.NEARBY_WIFI_DEVICES);
    }
}
