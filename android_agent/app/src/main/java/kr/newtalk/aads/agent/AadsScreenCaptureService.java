package kr.newtalk.aads.agent;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaRecorder;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.nio.ByteBuffer;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public class AadsScreenCaptureService {
    private static volatile int savedResultCode;
    private static volatile Intent savedResultData;
    private static volatile MediaProjection currentProjection;

    /** Called from MainActivity after user grants MediaProjection consent */
    public static void setProjectionResult(int resultCode, Intent data) {
        savedResultCode = resultCode;
        savedResultData = data;
    }

    public static boolean hasProjectionConsent() {
        return savedResultCode == Activity.RESULT_OK && savedResultData != null;
    }

    /** Capture a screenshot and return base64 JPEG */
    public static JSONObject captureScreenshot(Context context, JSONObject params) {
        if (!hasProjectionConsent()) {
            JSONObject data = new JSONObject();
            ResultJson.put(data, "error", "MediaProjection consent required");
            ResultJson.put(data, "user_visible_state", "projection_consent_needed");
            return ResultJson.error("MediaProjection consent not granted. Open the app and tap 'Enable Screenshot'.");
        }

        MediaProjectionManager mpm = (MediaProjectionManager) context.getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        if (mpm == null) return ResultJson.error("MediaProjectionManager unavailable");

        DisplayMetrics metrics = new DisplayMetrics();
        WindowManager wm = (WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        if (wm != null) wm.getDefaultDisplay().getRealMetrics(metrics);
        int width = params.optInt("width", metrics.widthPixels > 0 ? metrics.widthPixels : 1080);
        int height = params.optInt("height", metrics.heightPixels > 0 ? metrics.heightPixels : 2400);
        int density = metrics.densityDpi > 0 ? metrics.densityDpi : 420;
        int quality = Math.max(10, Math.min(params.optInt("quality", 70), 100));

        // Scale down for performance
        float scale = params.optDouble("scale", 0.5) > 0 ? (float) params.optDouble("scale", 0.5) : 0.5f;
        int scaledWidth = (int) (width * scale);
        int scaledHeight = (int) (height * scale);

        HandlerThread thread = new HandlerThread("AadsScreenCapture");
        thread.start();
        Handler handler = new Handler(thread.getLooper());

        MediaProjection projection = null;
        VirtualDisplay display = null;
        ImageReader reader = null;

        try {
            projection = mpm.getMediaProjection(savedResultCode, (Intent) savedResultData.clone());
            if (projection == null) return ResultJson.error("failed to create MediaProjection");
            currentProjection = projection;

            reader = ImageReader.newInstance(scaledWidth, scaledHeight, PixelFormat.RGBA_8888, 2);
            CountDownLatch latch = new CountDownLatch(1);
            AtomicReference<byte[]> bytesRef = new AtomicReference<>();

            ImageReader finalReader = reader;
            reader.setOnImageAvailableListener(r -> {
                Image image = null;
                try {
                    image = r.acquireLatestImage();
                    if (image == null) return;
                    Image.Plane[] planes = image.getPlanes();
                    ByteBuffer buffer = planes[0].getBuffer();
                    int pixelStride = planes[0].getPixelStride();
                    int rowStride = planes[0].getRowStride();
                    int rowPadding = rowStride - pixelStride * scaledWidth;

                    Bitmap bitmap = Bitmap.createBitmap(
                            scaledWidth + rowPadding / pixelStride, scaledHeight,
                            Bitmap.Config.ARGB_8888);
                    bitmap.copyPixelsFromBuffer(buffer);
                    bitmap = Bitmap.createBitmap(bitmap, 0, 0, scaledWidth, scaledHeight);

                    ByteArrayOutputStream baos = new ByteArrayOutputStream();
                    bitmap.compress(Bitmap.CompressFormat.JPEG, quality, baos);
                    bitmap.recycle();
                    bytesRef.set(baos.toByteArray());
                } catch (Exception ignored) {
                } finally {
                    if (image != null) image.close();
                    latch.countDown();
                }
            }, handler);

            display = projection.createVirtualDisplay(
                    "AadsScreenCapture",
                    scaledWidth, scaledHeight, density,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    reader.getSurface(), null, handler);

            boolean completed = latch.await(5, TimeUnit.SECONDS);
            if (!completed) return ResultJson.timeout("screenshot capture timed out");

            byte[] bytes = bytesRef.get();
            if (bytes == null || bytes.length == 0) return ResultJson.error("screenshot capture failed");

            String base64 = Base64.encodeToString(bytes, Base64.NO_WRAP);
            int maxChars = Math.max(128, Math.min(params.optInt("max_base64_chars", 50000), 500000));

            JSONObject data = new JSONObject();
            ResultJson.put(data, "width", scaledWidth);
            ResultJson.put(data, "height", scaledHeight);
            ResultJson.put(data, "bytes", bytes.length);
            ResultJson.put(data, "format", "jpeg");
            ResultJson.put(data, "quality", quality);
            ResultJson.put(data, "base64", base64.length() > maxChars ? base64.substring(0, maxChars) + "...(truncated)" : base64);
            return ResultJson.success(data);
        } catch (Exception e) {
            return ResultJson.error("screenshot error: " + (e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName()));
        } finally {
            if (display != null) display.release();
            if (projection != null) { projection.stop(); currentProjection = null; }
            if (reader != null) reader.close();
            thread.quitSafely();
        }
    }

    /** Start audio recording (microphone) */
    public static JSONObject startAudioRecording(Context context, JSONObject params) {
        int durationSeconds = Math.max(1, Math.min(params.optInt("duration_seconds", 10), 60));
        File outputFile = new File(context.getCacheDir(), "aads_audio_" + System.currentTimeMillis() + ".3gp");

        try {
            MediaRecorder recorder = new MediaRecorder();
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC);
            recorder.setOutputFormat(MediaRecorder.OutputFormat.THREE_GPP);
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AMR_NB);
            recorder.setOutputFile(outputFile.getAbsolutePath());
            recorder.setMaxDuration(durationSeconds * 1000);
            recorder.prepare();
            recorder.start();

            Thread.sleep(durationSeconds * 1000L);
            recorder.stop();
            recorder.release();

            byte[] audioBytes = readFileBytes(outputFile);
            String base64 = Base64.encodeToString(audioBytes, Base64.NO_WRAP);
            int maxChars = Math.max(128, Math.min(params.optInt("max_base64_chars", 100000), 500000));

            JSONObject data = new JSONObject();
            ResultJson.put(data, "duration_seconds", durationSeconds);
            ResultJson.put(data, "bytes", audioBytes.length);
            ResultJson.put(data, "format", "3gp");
            ResultJson.put(data, "base64", base64.length() > maxChars ? base64.substring(0, maxChars) + "...(truncated)" : base64);
            outputFile.delete();
            return ResultJson.success(data);
        } catch (Exception e) {
            outputFile.delete();
            return ResultJson.error("audio recording error: " + (e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName()));
        }
    }

    private static byte[] readFileBytes(File file) throws Exception {
        java.io.FileInputStream fis = new java.io.FileInputStream(file);
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int read;
        while ((read = fis.read(buffer)) != -1) baos.write(buffer, 0, read);
        fis.close();
        return baos.toByteArray();
    }
}
