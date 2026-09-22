package io.github.adrianlzr.spirebrain;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Polls the SpireBrain dashboard for the latest decision, off the render thread.
 *
 * Design notes:
 * - One long-lived thread, one blocking GET with a short timeout, then sleep.
 *   Not a thread pool: the payload is ~1 KB and 2 s cadence needs no parallelism.
 * - The server's /events is an infinite SSE stream; a poller instead wants a
 *   bounded answer. GET /health returns the event count; GET /state (added on
 *   the server for exactly this mod) returns the latest snapshot as JSON.
 * - Every failure path degrades to "keep showing the last snapshot" and, at
 *   most, a status line. Never an exception into the game's render loop.
 */
public final class FeedClient {

    /** What the mod renders. Immutable; swapped atomically by the poller. */
    public static final class Snapshot {
        public final String headline;   // "[point] -> value"
        public final String detail;     // fallback reason / gate reason (may be "")
        public final boolean fallback;  // true when a rule took over
        public final int hp, maxHp, budgetRemaining, budgetMax;

        Snapshot(String headline, String detail, boolean fallback,
                 int hp, int maxHp, int budgetRemaining, int budgetMax) {
            this.headline = headline;
            this.detail = detail;
            this.fallback = fallback;
            this.hp = hp;
            this.maxHp = maxHp;
            this.budgetRemaining = budgetRemaining;
            this.budgetMax = budgetMax;
        }
    }

    private final String baseUrl;
    private final Consumer<Snapshot> onSnapshot;
    private final Consumer<String> onStatus;
    private final Thread worker;
    private final AtomicBoolean running = new AtomicBoolean(true);
    private volatile long lastEventSeq = 0;

    public FeedClient(String baseUrl, Consumer<Snapshot> onSnapshot, Consumer<String> onStatus) {
        this.baseUrl = baseUrl == null ? "http://127.0.0.1:8787" : baseUrl.replaceAll("/+$", "");
        this.onSnapshot = onSnapshot;
        this.onStatus = onStatus;
        this.worker = new Thread(this::loop, "SpireBrainFeed");
        this.worker.setDaemon(true); // the game owns the process; we are a guest
        this.worker.start();
    }

    /** Called from the render thread: set the "want a poll" flag the worker honours. */
    public void pollAsync() {
        synchronized (this) {
            notifyAll();
        }
    }

    private void loop() {
        while (running.get()) {
            try {
                JSONObject state = getJson(baseUrl + "/state");
                if (state != null) {
                    Snapshot snap = parse(state);
                    if (snap != null) {
                        onSnapshot.accept(snap);
                    }
                }
                synchronized (this) {
                    wait(2000L);
                }
            } catch (InterruptedException ie) {
                return;
            } catch (Exception e) {
                onStatus.accept("SpireBrain: agent not reachable (" + e.getClass().getSimpleName() + ")");
                try {
                    synchronized (this) {
                        wait(5000L); // backing off; the dashboard may not be up yet
                    }
                } catch (InterruptedException ie) {
                    return;
                }
            }
        }
    }

    private Snapshot parse(JSONObject state) {
        JSONObject last = state.optJSONObject("last_decision");
        JSONObject run = state.optJSONObject("run_state");
        if (last == null && run == null) {
            return null;
        }
        String headline;
        boolean fallback = false;
        String detail = "";
        if (last != null) {
            String point = last.optString("point", "?");
            String value = String.valueOf(last.opt("value"));
            double conf = last.optDouble("confidence", 0.0);
            fallback = last.optBoolean("fallback", false);
            headline = String.format("[%s] -> %s   conf %.2f%s",
                    point, value, conf, fallback ? "  (rule took over)" : "");
            detail = last.optString("reason", "");
            if (detail.isEmpty()) {
                JSONObject gate = last.optJSONObject("gate");
                if (gate != null) {
                    detail = gate.optString("reason", "");
                }
            }
        } else {
            headline = "SpireBrain is watching the run…";
        }

        int hp = run != null ? run.optInt("hp", 0) : 0;
        int maxHp = run != null ? run.optInt("max_hp", 80) : 80;
        int remaining = run != null ? run.optInt("budget_remaining", 0) : 0;
        int reserved = run != null ? run.optInt("reserved", 0) : 0;
        return new Snapshot(headline, detail, fallback,
                hp, maxHp, remaining, Math.max(1, maxHp - reserved));
    }

    private JSONObject getJson(String url) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setConnectTimeout(800);
        conn.setReadTimeout(1500);
        conn.setRequestProperty("Connection", "close");
        int code = conn.getResponseCode();
        if (code != 200) {
            throw new IllegalStateException("HTTP " + code);
        }
        StringBuilder sb = new StringBuilder(4096);
        try (BufferedReader r = new BufferedReader(
                new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) {
                sb.append(line).append('\n');
            }
        } finally {
            conn.disconnect();
        }
        return new JSONObject(sb.toString());
    }
}
