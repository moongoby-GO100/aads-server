package kr.newtalk.aads.agent;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

final class VoiceWakeController {
    interface Callback {
        void onVoiceWakeStateChanged();
    }

    static final String STATUS_DISABLED = "disabled";
    static final String STATUS_LISTENING = "listening";
    static final String STATUS_TRIGGERED = "triggered";
    static final String STATUS_ERROR = "error";

    private static final String TAG = "VoiceWakeController";
    private static final String PREFS = "aads_voice_wake";
    private static final String KEY_ENABLED = "enabled";
    private static final String KEY_STATUS = "status";
    private static final String KEY_LAST_ERROR = "last_error";
    private static final String KEY_LAST_TEXT = "last_text";
    private static final String KEY_LAST_WAKE = "last_wake";
    private static final String[] WAKE_PHRASES = new String[]{
            "오비스",
            "오 비스",
            "ohvis",
            "obis",
            "aads"
    };

    private final Context context;
    private final Callback callback;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private SpeechRecognizer recognizer;
    private boolean running;
    private boolean listening;

    VoiceWakeController(Context context, Callback callback) {
        this.context = context.getApplicationContext();
        this.callback = callback;
    }

    static boolean isEnabled(Context context) {
        return prefs(context).getBoolean(KEY_ENABLED, false);
    }

    static void setEnabled(Context context, boolean enabled) {
        SharedPreferences.Editor editor = prefs(context).edit().putBoolean(KEY_ENABLED, enabled);
        if (!enabled) {
            editor.putString(KEY_STATUS, STATUS_DISABLED)
                    .putString(KEY_LAST_ERROR, "");
        }
        editor.apply();
    }

    static VoiceWakeState loadState(Context context) {
        SharedPreferences prefs = prefs(context);
        return new VoiceWakeState(
                prefs.getBoolean(KEY_ENABLED, false),
                prefs.getString(KEY_STATUS, STATUS_DISABLED),
                prefs.getString(KEY_LAST_ERROR, ""),
                prefs.getString(KEY_LAST_TEXT, ""),
                prefs.getLong(KEY_LAST_WAKE, 0L)
        );
    }

    void start() {
        setEnabled(context, true);
        handler.post(() -> {
            if (!PermissionGate.has(context, Manifest.permission.RECORD_AUDIO)) {
                running = false;
                listening = false;
                store(STATUS_ERROR, "record_audio permission required", "");
                return;
            }
            if (!SpeechRecognizer.isRecognitionAvailable(context)) {
                running = false;
                listening = false;
                store(STATUS_ERROR, "speech recognizer unavailable", "");
                return;
            }
            running = true;
            ensureRecognizer();
            scheduleListen(100L);
        });
    }

    void stop() {
        setEnabled(context, false);
        handler.post(() -> {
            running = false;
            listening = false;
            if (recognizer != null) {
                try {
                    recognizer.cancel();
                    recognizer.destroy();
                } catch (Exception e) {
                    Log.w(TAG, "Failed to destroy speech recognizer", e);
                }
                recognizer = null;
            }
            store(STATUS_DISABLED, "", "");
        });
    }

    void release() {
        handler.post(() -> {
            running = false;
            listening = false;
            if (recognizer != null) {
                try {
                    recognizer.destroy();
                } catch (Exception ignored) {
                }
                recognizer = null;
            }
        });
    }

    private void ensureRecognizer() {
        if (recognizer != null) {
            return;
        }
        recognizer = SpeechRecognizer.createSpeechRecognizer(context);
        recognizer.setRecognitionListener(new WakeRecognitionListener());
    }

    private void scheduleListen(long delayMs) {
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(this::listenOnce, delayMs);
    }

    private void listenOnce() {
        if (!running || recognizer == null || listening) {
            return;
        }
        try {
            listening = true;
            store(STATUS_LISTENING, "", "");
            recognizer.startListening(buildRecognizerIntent());
        } catch (Exception e) {
            listening = false;
            store(STATUS_ERROR, e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage(), "");
            scheduleListen(3_000L);
        }
    }

    private Intent buildRecognizerIntent() {
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.KOREA.toLanguageTag());
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5);
        intent.putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.getPackageName());
        return intent;
    }

    private boolean handleCandidates(List<String> candidates) {
        String combined = String.join(" ", candidates == null ? new ArrayList<>() : candidates);
        store(STATUS_LISTENING, "", combined);
        for (String candidate : candidates == null ? new ArrayList<String>() : candidates) {
            if (isWakePhrase(candidate)) {
                storeWake(candidate);
                openOhvis(candidate);
                return true;
            }
        }
        return false;
    }

    private boolean isWakePhrase(String raw) {
        String normalized = normalize(raw);
        if (normalized.isEmpty()) {
            return false;
        }
        for (String phrase : WAKE_PHRASES) {
            String wake = normalize(phrase);
            if (normalized.contains(wake)) {
                return true;
            }
        }
        return false;
    }

    private String normalize(String value) {
        return String.valueOf(value == null ? "" : value)
                .toLowerCase(Locale.ROOT)
                .replace(" ", "")
                .replace("-", "")
                .trim();
    }

    private void openOhvis(String phrase) {
        Intent intent = new Intent(context, MainActivity.class);
        intent.setAction(MainActivity.ACTION_WAKE_FROM_VOICE);
        intent.setData(android.net.Uri.parse("ohvis://wake?source=voice"));
        intent.putExtra("wake_phrase", phrase);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        context.startActivity(intent);
    }

    private void storeWake(String phrase) {
        prefs(context).edit()
                .putString(KEY_STATUS, STATUS_TRIGGERED)
                .putString(KEY_LAST_ERROR, "")
                .putString(KEY_LAST_TEXT, phrase == null ? "" : phrase)
                .putLong(KEY_LAST_WAKE, System.currentTimeMillis())
                .apply();
        callback.onVoiceWakeStateChanged();
    }

    private void store(String status, String error, String text) {
        prefs(context).edit()
                .putString(KEY_STATUS, status == null ? "" : status)
                .putString(KEY_LAST_ERROR, error == null ? "" : error)
                .putString(KEY_LAST_TEXT, text == null ? "" : text)
                .apply();
        callback.onVoiceWakeStateChanged();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private final class WakeRecognitionListener implements RecognitionListener {
        @Override
        public void onReadyForSpeech(Bundle params) {
            store(STATUS_LISTENING, "", "");
        }

        @Override
        public void onBeginningOfSpeech() {
        }

        @Override
        public void onRmsChanged(float rmsdB) {
        }

        @Override
        public void onBufferReceived(byte[] buffer) {
        }

        @Override
        public void onEndOfSpeech() {
            listening = false;
        }

        @Override
        public void onError(int error) {
            listening = false;
            String message = errorMessage(error);
            store(STATUS_ERROR, message, "");
            if (running && error != SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                scheduleListen(error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY ? 3_000L : 1_500L);
            }
        }

        @Override
        public void onResults(Bundle results) {
            listening = false;
            boolean triggered = handleCandidates(results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
            if (running) {
                scheduleListen(triggered ? 2_500L : 400L);
            }
        }

        @Override
        public void onPartialResults(Bundle partialResults) {
            handleCandidates(partialResults.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION));
        }

        @Override
        public void onEvent(int eventType, Bundle params) {
        }
    }

    private String errorMessage(int error) {
        switch (error) {
            case SpeechRecognizer.ERROR_AUDIO:
                return "audio capture failed";
            case SpeechRecognizer.ERROR_CLIENT:
                return "speech recognizer client error";
            case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                return "record_audio permission required";
            case SpeechRecognizer.ERROR_NETWORK:
                return "speech network error";
            case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
                return "speech network timeout";
            case SpeechRecognizer.ERROR_NO_MATCH:
                return "no wake phrase matched";
            case SpeechRecognizer.ERROR_RECOGNIZER_BUSY:
                return "speech recognizer busy";
            case SpeechRecognizer.ERROR_SERVER:
                return "speech service error";
            case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                return "speech timeout";
            default:
                return "speech error " + error;
        }
    }
}
