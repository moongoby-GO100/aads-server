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
