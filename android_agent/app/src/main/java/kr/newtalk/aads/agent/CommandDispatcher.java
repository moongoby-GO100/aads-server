package kr.newtalk.aads.agent;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;

final class CommandDispatcher {
    private final Map<String, CommandHandler> handlers = new LinkedHashMap<>();

    private CommandDispatcher() {
    }

    static CommandDispatcher create(Context context) {
        CommandDispatcher dispatcher = new CommandDispatcher();
        dispatcher.register("battery", params -> AndroidCommandHandlers.battery(context));
        dispatcher.register("location", params -> AndroidCommandHandlers.location(context, params));
        dispatcher.register("camera", params -> AndroidCommandHandlers.camera(context, params));
        dispatcher.register("camera_photo", params -> AndroidCommandHandlers.camera(context, params));
        dispatcher.register("notification", params -> AndroidCommandHandlers.notification(context, params));
        dispatcher.register("notification_send", params -> AndroidCommandHandlers.notification(context, params));
        dispatcher.register("clipboard", params -> AndroidCommandHandlers.clipboard(context, params, ""));
        dispatcher.register("clipboard_get", params -> AndroidCommandHandlers.clipboard(context, params, "get"));
        dispatcher.register("clipboard_set", params -> AndroidCommandHandlers.clipboard(context, params, "set"));
        dispatcher.register("vibrate", params -> AndroidCommandHandlers.vibrate(context, params));
        dispatcher.register("tts", params -> AndroidCommandHandlers.tts(context, params));
        dispatcher.register("tts_speak", params -> AndroidCommandHandlers.tts(context, params));
        dispatcher.register("volume", params -> AndroidCommandHandlers.volume(context, params));
        dispatcher.register("volume_set", params -> AndroidCommandHandlers.volume(context, params));
        dispatcher.register("wifi", params -> AndroidCommandHandlers.wifi(context, params, ""));
        dispatcher.register("wifi_info", params -> AndroidCommandHandlers.wifi(context, params, "info"));
        dispatcher.register("wifi_scan", params -> AndroidCommandHandlers.wifi(context, params, "scan"));
        dispatcher.register("shell_limited", params -> AndroidCommandHandlers.shellLimited(params));
        dispatcher.register("shell", params -> AndroidCommandHandlers.shellLimited(params));
        dispatcher.register("sms_send", params -> AndroidCommandHandlers.smsSend(context, params));
        dispatcher.register("call_dial", params -> AndroidCommandHandlers.callDial(context, params));
        dispatcher.register("call", params -> AndroidCommandHandlers.callDial(context, params));
        dispatcher.register("contacts_list", params -> AndroidCommandHandlers.contactsList(context, params));
        dispatcher.register("contacts", params -> AndroidCommandHandlers.contactsList(context, params));
        dispatcher.register("contacts_search", params -> AndroidCommandHandlers.contactsSearch(context, params));
        dispatcher.register("call_log", params -> AndroidCommandHandlers.callLog(context, params));
        dispatcher.register("sms_inbox", params -> AndroidCommandHandlers.smsInbox(context, params));
        dispatcher.register("photo_gallery", params -> AndroidCommandHandlers.photoGallery(context, params));
        dispatcher.register("photos", params -> AndroidCommandHandlers.photoGallery(context, params));
        dispatcher.register("app_list", params -> AndroidCommandHandlers.appList(context));
        dispatcher.register("apps", params -> AndroidCommandHandlers.appList(context));
        dispatcher.register("app_launch", params -> AndroidCommandHandlers.appLaunch(context, params));
        dispatcher.register("bluetooth_status", params -> AndroidCommandHandlers.bluetoothStatus(context, params));
        dispatcher.register("bluetooth", params -> AndroidCommandHandlers.bluetoothStatus(context, params));
        dispatcher.register("screen_tap", params -> AndroidCommandHandlers.screenTap(params));
        dispatcher.register("tap", params -> AndroidCommandHandlers.screenTap(params));
        dispatcher.register("screen_swipe", params -> AndroidCommandHandlers.screenSwipe(params));
        dispatcher.register("swipe", params -> AndroidCommandHandlers.screenSwipe(params));
        dispatcher.register("screen_long_press", params -> AndroidCommandHandlers.screenLongPress(params));
        dispatcher.register("long_press", params -> AndroidCommandHandlers.screenLongPress(params));
        dispatcher.register("screen_text", params -> AndroidCommandHandlers.screenText(params));
        dispatcher.register("key_input", params -> AndroidCommandHandlers.keyInput(params));
        dispatcher.register("global_action", params -> AndroidCommandHandlers.globalAction(params));
        dispatcher.register("find_and_click", params -> AndroidCommandHandlers.findAndClick(params));
        dispatcher.register("screen_scroll", params -> AndroidCommandHandlers.screenScroll(params));
        dispatcher.register("sensor_data", params -> AndroidCommandHandlers.sensorData(context));
        dispatcher.register("sensors", params -> AndroidCommandHandlers.sensorData(context));
        dispatcher.register("notification_read", params -> AndroidCommandHandlers.notificationRead(context, params));
        dispatcher.register("notifications_read", params -> AndroidCommandHandlers.notificationRead(context, params));
        dispatcher.register("screenshot", params -> AndroidCommandHandlers.screenshot(context, params));
        dispatcher.register("device_lock", params -> AndroidCommandHandlers.deviceLock(context, params));
        dispatcher.register("device_wipe", params -> AndroidCommandHandlers.deviceWipe(context, params));
        dispatcher.register("device_admin_status", params -> AndroidCommandHandlers.deviceAdminStatus(context));
        dispatcher.register("screen_brightness", params -> AndroidCommandHandlers.screenBrightness(context, params));
        dispatcher.register("screen_timeout", params -> AndroidCommandHandlers.screenTimeout(context, params));
        dispatcher.register("audio_record", params -> AndroidCommandHandlers.audioRecord(context, params));
        return dispatcher;
    }

    JSONObject dispatch(String commandType, JSONObject params) {
        CommandHandler handler = handlers.get(commandType);
        if (handler == null) {
            return ResultJson.error("unsupported command: " + commandType);
        }
        try {
            return handler.handle(params == null ? new JSONObject() : params);
        } catch (Exception e) {
            return ResultJson.error(e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
        }
    }

    JSONArray capabilities() {
        JSONArray array = new JSONArray();
        for (String key : handlers.keySet()) {
            array.put(key);
        }
        return array;
    }

    private void register(String commandType, CommandHandler handler) {
        handlers.put(commandType, handler);
    }
}
