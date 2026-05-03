package kr.newtalk.aads.agent;

import android.app.Notification;
import android.content.Context;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

public final class AadsNotificationListener extends NotificationListenerService {
    private static final int MAX_RECENT = 100;
    private static final List<JSONObject> RECENT = new ArrayList<>();
    private static volatile AadsNotificationListener instance;

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        instance = this;
    }

    @Override
    public void onListenerDisconnected() {
        instance = null;
        super.onListenerDisconnected();
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        JSONObject item = notificationToJson(sbn);
        synchronized (RECENT) {
            RECENT.add(0, item);
            while (RECENT.size() > MAX_RECENT) {
                RECENT.remove(RECENT.size() - 1);
            }
        }
    }

    static JSONArray recentNotifications(int limit) {
        JSONArray array = new JSONArray();
        AadsNotificationListener listener = instance;
        if (listener != null) {
            appendActiveNotifications(listener, array, limit);
        }
        synchronized (RECENT) {
            int count = Math.min(Math.max(0, limit - array.length()), RECENT.size());
            for (int i = 0; i < count; i++) {
                try {
                    array.put(new JSONObject(RECENT.get(i).toString()));
                } catch (Exception ignored) {
                }
            }
        }
        return array;
    }

    static boolean isEnabled(Context context) {
        String enabled = Settings.Secure.getString(context.getContentResolver(), "enabled_notification_listeners");
        return enabled != null && enabled.toLowerCase(Locale.US).contains(context.getPackageName().toLowerCase(Locale.US));
    }

    private static void appendActiveNotifications(AadsNotificationListener listener, JSONArray array, int limit) {
        try {
            StatusBarNotification[] notifications = listener.getActiveNotifications();
            if (notifications == null) {
                return;
            }
            for (StatusBarNotification notification : notifications) {
                if (array.length() >= limit) {
                    return;
                }
                array.put(notificationToJson(notification));
            }
        } catch (Exception ignored) {
        }
    }

    private static JSONObject notificationToJson(StatusBarNotification sbn) {
        Notification notification = sbn.getNotification();
        JSONObject item = new JSONObject();
        ResultJson.put(item, "package", sbn.getPackageName());
        ResultJson.put(item, "id", sbn.getId());
        ResultJson.put(item, "tag", sbn.getTag() == null ? "" : sbn.getTag());
        ResultJson.put(item, "key", sbn.getKey());
        ResultJson.put(item, "post_time", sbn.getPostTime());
        ResultJson.put(item, "is_ongoing", sbn.isOngoing());
        if (notification != null && notification.extras != null) {
            ResultJson.put(item, "title", charSequence(notification.extras.getCharSequence(Notification.EXTRA_TITLE)));
            ResultJson.put(item, "text", charSequence(notification.extras.getCharSequence(Notification.EXTRA_TEXT)));
            ResultJson.put(item, "sub_text", charSequence(notification.extras.getCharSequence(Notification.EXTRA_SUB_TEXT)));
            ResultJson.put(item, "big_text", charSequence(notification.extras.getCharSequence(Notification.EXTRA_BIG_TEXT)));
        } else {
            ResultJson.put(item, "title", "");
            ResultJson.put(item, "text", "");
            ResultJson.put(item, "sub_text", "");
            ResultJson.put(item, "big_text", "");
        }
        return item;
    }

    private static String charSequence(CharSequence value) {
        return value == null ? "" : value.toString();
    }
}
