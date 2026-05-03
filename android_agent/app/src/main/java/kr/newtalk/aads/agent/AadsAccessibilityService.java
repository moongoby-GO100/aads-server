package kr.newtalk.aads.agent;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public final class AadsAccessibilityService extends AccessibilityService {
    private static volatile AadsAccessibilityService instance;

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
    }

    public static AadsAccessibilityService getInstance() {
        return instance;
    }

    public static boolean isRunning() {
        return instance != null;
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        if (instance == this) {
            instance = null;
        }
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        if (instance == this) {
            instance = null;
        }
        super.onDestroy();
    }

    static boolean isReady() {
        return instance != null;
    }

    static GestureOutcome tap(float x, float y) throws InterruptedException {
        Path path = new Path();
        path.moveTo(x, y);
        return dispatchGesture(path, 0, 80, 2500);
    }

    static GestureOutcome swipe(float x1, float y1, float x2, float y2, long durationMs) throws InterruptedException {
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        return dispatchGesture(path, 0, durationMs, durationMs + 2500);
    }

    static GestureOutcome longPress(float x, float y, long durationMs) throws InterruptedException {
        Path path = new Path();
        path.moveTo(x, y);
        return dispatchGesture(path, 0, durationMs, durationMs + 2500);
    }

    static GestureOutcome dispatchGesture(Path path, long startMs, long durationMs, long timeoutMs) throws InterruptedException {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return GestureOutcome.unavailable("accessibility service not enabled");
        }
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, startMs, Math.max(1, durationMs)))
                .build();
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<Boolean> completed = new AtomicReference<>(false);
        AtomicReference<Boolean> cancelled = new AtomicReference<>(false);
        boolean accepted = service.dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription gestureDescription) {
                completed.set(true);
                latch.countDown();
            }

            @Override
            public void onCancelled(GestureDescription gestureDescription) {
                cancelled.set(true);
                latch.countDown();
            }
        }, new Handler(Looper.getMainLooper()));
        if (!accepted) {
            return new GestureOutcome(true, false, false, false, "gesture dispatch rejected");
        }
        latch.await(Math.max(500, timeoutMs), TimeUnit.MILLISECONDS);
        return new GestureOutcome(true, true, completed.get(), cancelled.get(), "");
    }

    static ScreenTextResult getScreenText(int maxNodes) {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return ScreenTextResult.unavailable("accessibility service not enabled");
        }
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) {
            return ScreenTextResult.unavailable("active window unavailable");
        }
        JSONArray nodes = new JSONArray();
        StringBuilder text = new StringBuilder();
        try {
            collectText(root, nodes, text, maxNodes);
        } finally {
            root.recycle();
        }
        return new ScreenTextResult(true, text.toString().trim(), nodes, "");
    }

    static AccessibilityNodeInfo findNodeByText(String text) {
        AadsAccessibilityService service = instance;
        if (service == null || text == null || text.trim().isEmpty()) {
            return null;
        }
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) {
            return null;
        }
        try {
            List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByText(text);
            if (nodes == null) {
                return null;
            }
            try {
                for (AccessibilityNodeInfo node : nodes) {
                    if (nodeMatchesText(node, text)) {
                        return AccessibilityNodeInfo.obtain(node);
                    }
                }
                return nodes.isEmpty() ? null : AccessibilityNodeInfo.obtain(nodes.get(0));
            } finally {
                for (AccessibilityNodeInfo node : nodes) {
                    node.recycle();
                }
            }
        } finally {
            root.recycle();
        }
    }

    static AccessibilityNodeInfo findNodeByViewId(String viewId) {
        AadsAccessibilityService service = instance;
        if (service == null || viewId == null || viewId.trim().isEmpty()) {
            return null;
        }
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) {
            return null;
        }
        try {
            try {
                List<AccessibilityNodeInfo> nodes = root.findAccessibilityNodeInfosByViewId(viewId);
                if (nodes != null && !nodes.isEmpty()) {
                    try {
                        return AccessibilityNodeInfo.obtain(nodes.get(0));
                    } finally {
                        for (AccessibilityNodeInfo node : nodes) {
                            node.recycle();
                        }
                    }
                }
                recycleNodes(nodes);
            } catch (IllegalArgumentException ignored) {
            }
            return findNodeByViewIdDeep(root, viewId);
        } finally {
            root.recycle();
        }
    }

    static InputOutcome inputText(String text, boolean append) {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return InputOutcome.unavailable("accessibility service not enabled");
        }
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        if (root == null) {
            return InputOutcome.unavailable("active window unavailable");
        }
        AccessibilityNodeInfo focused = null;
        try {
            focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (focused == null) {
                return InputOutcome.unavailable("input focus unavailable");
            }
            CharSequence current = focused.getText();
            String next = append && current != null ? current + text : text;
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, next);
            return new InputOutcome(true, focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args), "");
        } finally {
            if (focused != null) {
                focused.recycle();
            }
            root.recycle();
        }
    }

    static GlobalActionOutcome performGlobalAction(String actionName) {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return GlobalActionOutcome.unavailable("accessibility service not enabled");
        }
        int action = globalActionCode(actionName);
        if (action < 0) {
            return GlobalActionOutcome.unavailable("unsupported global action");
        }
        boolean completed = service.performGlobalAction(action);
        return new GlobalActionOutcome(true, normalizeAction(actionName), action, completed, "");
    }

    static ClickOutcome findAndClickByText(String text) {
        AccessibilityNodeInfo node = findNodeByText(text);
        if (node == null) {
            return isReady()
                    ? ClickOutcome.unavailable("matching text not found")
                    : ClickOutcome.unavailable("accessibility service not enabled");
        }
        return clickNode(node);
    }

    static ClickOutcome findAndClickByViewId(String viewId) {
        AccessibilityNodeInfo node = findNodeByViewId(viewId);
        if (node == null) {
            return isReady()
                    ? ClickOutcome.unavailable("matching view id not found")
                    : ClickOutcome.unavailable("accessibility service not enabled");
        }
        return clickNode(node);
    }

    static ScrollOutcome scroll(String direction) throws InterruptedException {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return ScrollOutcome.unavailable("accessibility service not enabled");
        }
        String normalized = direction == null || direction.trim().isEmpty()
                ? "down"
                : direction.trim().toLowerCase(Locale.US);
        if (!"up".equals(normalized) && !"down".equals(normalized)
                && !"left".equals(normalized) && !"right".equals(normalized)) {
            return ScrollOutcome.unavailable("unsupported direction");
        }
        AccessibilityNodeInfo root = service.getRootInActiveWindow();
        AccessibilityNodeInfo scrollable = root == null ? null : findScrollable(root);
        try {
            int action = ("up".equals(normalized) || "left".equals(normalized))
                    ? AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                    : AccessibilityNodeInfo.ACTION_SCROLL_FORWARD;
            if (scrollable != null && scrollable.performAction(action)) {
                return new ScrollOutcome(true, normalized, true, "accessibility_action", "");
            }
        } finally {
            if (scrollable != null) {
                scrollable.recycle();
            }
            if (root != null) {
                root.recycle();
            }
        }
        GestureOutcome fallback = fallbackScrollGesture(service, normalized);
        return new ScrollOutcome(fallback.available, normalized, fallback.completed, "gesture", fallback.error);
    }

    static ScreenshotOutcome takeScreenshotBase64(long timeoutMs) throws InterruptedException {
        AadsAccessibilityService service = instance;
        if (service == null) {
            return ScreenshotOutcome.unavailable("accessibility service not enabled");
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            return ScreenshotOutcome.unavailable("accessibility screenshot requires Android 11 or newer");
        }
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<ScreenshotOutcome> result = new AtomicReference<>();
        service.takeScreenshot(Display.DEFAULT_DISPLAY, command -> new Handler(Looper.getMainLooper()).post(command), new AccessibilityService.TakeScreenshotCallback() {
            @Override
            public void onSuccess(AccessibilityService.ScreenshotResult screenshotResult) {
                HardwareBuffer buffer = screenshotResult.getHardwareBuffer();
                Bitmap hardwareBitmap = null;
                Bitmap bitmap = null;
                try {
                    hardwareBitmap = Bitmap.wrapHardwareBuffer(buffer, screenshotResult.getColorSpace());
                    if (hardwareBitmap == null) {
                        result.set(ScreenshotOutcome.unavailable("screenshot bitmap unavailable"));
                    } else {
                        bitmap = hardwareBitmap.copy(Bitmap.Config.ARGB_8888, false);
                        ByteArrayOutputStream output = new ByteArrayOutputStream();
                        bitmap.compress(Bitmap.CompressFormat.PNG, 100, output);
                        byte[] bytes = output.toByteArray();
                        result.set(new ScreenshotOutcome(true, bitmap.getWidth(), bitmap.getHeight(), bytes.length,
                                android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP), ""));
                    }
                } catch (Exception e) {
                    result.set(ScreenshotOutcome.unavailable(e.getMessage() == null ? "screenshot failed" : e.getMessage()));
                } finally {
                    if (bitmap != null) {
                        bitmap.recycle();
                    }
                    if (hardwareBitmap != null) {
                        hardwareBitmap.recycle();
                    }
                    if (buffer != null) {
                        buffer.close();
                    }
                    latch.countDown();
                }
            }

            @Override
            public void onFailure(int errorCode) {
                result.set(ScreenshotOutcome.unavailable("screenshot failed: " + errorCode));
                latch.countDown();
            }
        });
        latch.await(timeoutMs, TimeUnit.MILLISECONDS);
        ScreenshotOutcome outcome = result.get();
        return outcome == null ? ScreenshotOutcome.unavailable("screenshot timed out") : outcome;
    }

    private static void collectText(AccessibilityNodeInfo node, JSONArray nodes, StringBuilder text, int maxNodes) {
        if (node == null || nodes.length() >= maxNodes) {
            return;
        }
        CharSequence nodeText = node.getText();
        CharSequence description = node.getContentDescription();
        String nodeTextString = nodeText == null ? "" : nodeText.toString();
        String descriptionString = description == null ? "" : description.toString();
        if (!nodeTextString.isEmpty() || !descriptionString.isEmpty()) {
            JSONObject item = new JSONObject();
            ResultJson.put(item, "text", nodeTextString);
            ResultJson.put(item, "content_description", descriptionString);
            ResultJson.put(item, "view_id", node.getViewIdResourceName());
            ResultJson.put(item, "class_name", node.getClassName() == null ? "" : node.getClassName().toString());
            ResultJson.put(item, "clickable", node.isClickable());
            nodes.put(item);
            if (!nodeTextString.isEmpty()) {
                if (text.length() > 0) {
                    text.append('\n');
                }
                text.append(nodeTextString);
            }
        }
        for (int i = 0; i < node.getChildCount() && nodes.length() < maxNodes; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) {
                try {
                    collectText(child, nodes, text, maxNodes);
                } finally {
                    child.recycle();
                }
            }
        }
    }

    private static boolean nodeMatchesText(AccessibilityNodeInfo node, String text) {
        String needle = text.toLowerCase(Locale.US);
        CharSequence nodeText = node.getText();
        CharSequence description = node.getContentDescription();
        return (nodeText != null && nodeText.toString().toLowerCase(Locale.US).contains(needle))
                || (description != null && description.toString().toLowerCase(Locale.US).contains(needle));
    }

    private static AccessibilityNodeInfo findNodeByViewIdDeep(AccessibilityNodeInfo node, String viewId) {
        if (node == null) {
            return null;
        }
        String nodeId = node.getViewIdResourceName();
        if (viewIdMatches(nodeId, viewId)) {
            return AccessibilityNodeInfo.obtain(node);
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) {
                continue;
            }
            try {
                AccessibilityNodeInfo found = findNodeByViewIdDeep(child, viewId);
                if (found != null) {
                    return found;
                }
            } finally {
                child.recycle();
            }
        }
        return null;
    }

    private static boolean viewIdMatches(String nodeId, String requestedId) {
        if (nodeId == null || requestedId == null) {
            return false;
        }
        String requested = requestedId.trim();
        return nodeId.equals(requested)
                || nodeId.endsWith(requested)
                || nodeId.endsWith("/" + requested)
                || nodeId.endsWith(":id/" + requested);
    }

    private static void recycleNodes(List<AccessibilityNodeInfo> nodes) {
        if (nodes == null) {
            return;
        }
        for (AccessibilityNodeInfo node : nodes) {
            node.recycle();
        }
    }

    private static ClickOutcome clickNode(AccessibilityNodeInfo node) {
        String text = node.getText() == null ? "" : node.getText().toString();
        String viewId = node.getViewIdResourceName();
        AccessibilityNodeInfo current = AccessibilityNodeInfo.obtain(node);
        node.recycle();
        try {
            while (current != null) {
                if (current.isClickable() && current.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    current.recycle();
                    current = null;
                    return new ClickOutcome(true, true, text, viewId, "");
                }
                AccessibilityNodeInfo parent = current.getParent();
                current.recycle();
                current = parent;
            }
            return new ClickOutcome(true, false, text, viewId, "clickable parent not found");
        } finally {
            if (current != null) {
                current.recycle();
            }
        }
    }

    private static AccessibilityNodeInfo findScrollable(AccessibilityNodeInfo node) {
        if (node == null) {
            return null;
        }
        if (node.isScrollable()) {
            return AccessibilityNodeInfo.obtain(node);
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) {
                continue;
            }
            try {
                AccessibilityNodeInfo found = findScrollable(child);
                if (found != null) {
                    return found;
                }
            } finally {
                child.recycle();
            }
        }
        return null;
    }

    private static GestureOutcome fallbackScrollGesture(AadsAccessibilityService service, String direction) throws InterruptedException {
        int width = service.getResources().getDisplayMetrics().widthPixels;
        int height = service.getResources().getDisplayMetrics().heightPixels;
        float x1 = width * 0.5f;
        float y1 = height * 0.75f;
        float x2 = width * 0.5f;
        float y2 = height * 0.25f;
        if ("up".equals(direction)) {
            y1 = height * 0.25f;
            y2 = height * 0.75f;
        } else if ("left".equals(direction)) {
            x1 = width * 0.25f;
            y1 = height * 0.5f;
            x2 = width * 0.75f;
            y2 = height * 0.5f;
        } else if ("right".equals(direction)) {
            x1 = width * 0.75f;
            y1 = height * 0.5f;
            x2 = width * 0.25f;
            y2 = height * 0.5f;
        }
        return swipe(x1, y1, x2, y2, 450);
    }

    private static int globalActionCode(String actionName) {
        switch (normalizeAction(actionName)) {
            case "back":
            case "global_action_back":
                return GLOBAL_ACTION_BACK;
            case "home":
            case "global_action_home":
                return GLOBAL_ACTION_HOME;
            case "recents":
            case "recent_apps":
            case "global_action_recents":
                return GLOBAL_ACTION_RECENTS;
            case "notifications":
            case "notification_shade":
            case "global_action_notifications":
                return GLOBAL_ACTION_NOTIFICATIONS;
            case "quick_settings":
            case "global_action_quick_settings":
                return GLOBAL_ACTION_QUICK_SETTINGS;
            case "power_dialog":
            case "global_action_power_dialog":
                return GLOBAL_ACTION_POWER_DIALOG;
            case "lock_screen":
            case "global_action_lock_screen":
                return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? GLOBAL_ACTION_LOCK_SCREEN : -1;
            case "take_screenshot":
            case "global_action_take_screenshot":
                return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? GLOBAL_ACTION_TAKE_SCREENSHOT : -1;
            default:
                return -1;
        }
    }

    private static String normalizeAction(String actionName) {
        return actionName == null
                ? ""
                : actionName.trim().toLowerCase(Locale.US).replace('-', '_').replace(' ', '_');
    }

    static final class GestureOutcome {
        final boolean available;
        final boolean accepted;
        final boolean completed;
        final boolean cancelled;
        final String error;

        GestureOutcome(boolean available, boolean accepted, boolean completed, boolean cancelled, String error) {
            this.available = available;
            this.accepted = accepted;
            this.completed = completed;
            this.cancelled = cancelled;
            this.error = error;
        }

        static GestureOutcome unavailable(String error) {
            return new GestureOutcome(false, false, false, false, error);
        }
    }

    static final class ScreenTextResult {
        final boolean available;
        final String text;
        final JSONArray nodes;
        final String error;

        ScreenTextResult(boolean available, String text, JSONArray nodes, String error) {
            this.available = available;
            this.text = text;
            this.nodes = nodes;
            this.error = error;
        }

        static ScreenTextResult unavailable(String error) {
            return new ScreenTextResult(false, "", new JSONArray(), error);
        }
    }

    static final class InputOutcome {
        final boolean available;
        final boolean completed;
        final String error;

        InputOutcome(boolean available, boolean completed, String error) {
            this.available = available;
            this.completed = completed;
            this.error = error;
        }

        static InputOutcome unavailable(String error) {
            return new InputOutcome(false, false, error);
        }
    }

    static final class GlobalActionOutcome {
        final boolean available;
        final String actionName;
        final int actionCode;
        final boolean completed;
        final String error;

        GlobalActionOutcome(boolean available, String actionName, int actionCode, boolean completed, String error) {
            this.available = available;
            this.actionName = actionName;
            this.actionCode = actionCode;
            this.completed = completed;
            this.error = error;
        }

        static GlobalActionOutcome unavailable(String error) {
            return new GlobalActionOutcome(false, "", -1, false, error);
        }
    }

    static final class ClickOutcome {
        final boolean available;
        final boolean completed;
        final String text;
        final String viewId;
        final String error;

        ClickOutcome(boolean available, boolean completed, String text, String viewId, String error) {
            this.available = available;
            this.completed = completed;
            this.text = text;
            this.viewId = viewId == null ? "" : viewId;
            this.error = error;
        }

        static ClickOutcome unavailable(String error) {
            return new ClickOutcome(false, false, "", "", error);
        }
    }

    static final class ScrollOutcome {
        final boolean available;
        final String direction;
        final boolean completed;
        final String method;
        final String error;

        ScrollOutcome(boolean available, String direction, boolean completed, String method, String error) {
            this.available = available;
            this.direction = direction;
            this.completed = completed;
            this.method = method;
            this.error = error;
        }

        static ScrollOutcome unavailable(String error) {
            return new ScrollOutcome(false, "", false, "", error);
        }
    }

    static final class ScreenshotOutcome {
        final boolean available;
        final int width;
        final int height;
        final int bytes;
        final String base64;
        final String error;

        ScreenshotOutcome(boolean available, int width, int height, int bytes, String base64, String error) {
            this.available = available;
            this.width = width;
            this.height = height;
            this.bytes = bytes;
            this.base64 = base64;
            this.error = error;
        }

        static ScreenshotOutcome unavailable(String error) {
            return new ScreenshotOutcome(false, 0, 0, 0, "", error);
        }
    }
}
