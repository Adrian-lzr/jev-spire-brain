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
 * Polls the SpireBrain dashboard for the latest state, off the render thread.
 *
 * Design notes:
 * - One long-lived thread, one blocking GET with a short timeout, then sleep.
 *   Not a thread pool: the payload is ~2 KB and a 1 s cadence needs no
 *   parallelism.
 * - The server's /events is an infinite SSE stream; a poller instead wants a
 *   bounded answer. GET /state returns the latest snapshot as JSON, built for
 *   this mod: the last decision, the last recommendation, the verdict on what
 *   the player did with it, and the run state.
 * - Every failure path degrades to "keep showing the last snapshot" and, at
 *   most, a status line. Never an exception into the game's render loop.
 *
 * ADVISOR MODE (2026-09-22). The agent has two modes and this poller serves both:
 *
 *   advise (the default) — `last_advice` carries a Chinese recommendation
 *                          ("出「痛击」→ 咔咔"), and `last_outcome` carries what the
 *                          player actually did. This mod shows both, which is the
 *                          whole point: a coach you have to alt-tab to read is
 *                          not a coach.
 *   play                 — there is no advice, only decisions; the panel falls
 *                          back to the decision headline it always showed.
 *
 * The fields are parsed defensively and BOTH a Chinese and an ASCII form are
 * kept for every label. Which one is drawn is decided by the overlay, which is
 * the only side that can ask the game's font whether it can render a glyph.
 */
public final class FeedClient {

    /** What the mod renders. One instance per poll; fields are never mutated. */
    public static final class Snapshot {
        // -- advisor mode -------------------------------------------------- //
        /** The recommendation in Chinese, e.g. 出「痛击」→ 咔咔. "" when none. */
        public String adviceLabel = "";
        /** The same recommendation in ASCII, for a font that cannot do Chinese. */
        public String adviceAscii = "";
        /** Which decision point it is: Chinese + ASCII pair. */
        public String pointLabel = "";
        public String pointAscii = "";
        /** Why (the router's own reason — English, so ASCII-safe). */
        public String adviceReason = "";
        public double adviceConfidence = 0.0;
        /** True when confidence is 0: rules answered, the model was not asked. */
        public boolean ruleOnly = false;
        public boolean fallback = false;
        public boolean hasAdvice = false;

        // -- what the player did with it ----------------------------------- //
        /** match | mismatch | unobserved. "" when nothing was judged. */
        public String verdict = "";
        /** Chinese + ASCII forms of the player's action. */
        public String actedLabel = "";
        public String actedAscii = "";
        /** "62%  采纳 8 · 未采纳 5 · 无法判定 3" (ASCII fallback included). */
        public String tallyText = "";
        public boolean hasOutcome = false;

        // -- run state ------------------------------------------------------ //
        public int hp, maxHp, budgetRemaining, budgetMax;

        // -- agent presence ------------------------------------------------- //
        /** "waiting_for_run": the agent is ALIVE at the main menu, not absent. */
        public String agentState = "";
        /** What to tell the player about that state (Chinese). */
        public String agentDetail = "";

        // -- play-mode fallback (no advice in the payload) ------------------ //
        public String headline = "";
        public String detail = "";
    }

    private final String baseUrl;
    private final Consumer<Snapshot> onSnapshot;
    private final Consumer<String> onStatus;
    private final Thread worker;
    private final AtomicBoolean running = new AtomicBoolean(true);

    public FeedClient(String baseUrl, Consumer<Snapshot> onSnapshot, Consumer<String> onStatus) {
        this.baseUrl = baseUrl == null ? "http://127.0.0.1:8787" : baseUrl.replaceAll("/+$", "");
        this.onSnapshot = onSnapshot;
        this.onStatus = onStatus;
        this.worker = new Thread(this::loop, "SpireBrainFeed");
        this.worker.setDaemon(true); // the game owns the process; we are a guest
        this.worker.start();
    }

    /** Called from the render thread: wake the poller (it sleeps between polls). */
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
                    } else {
                        // Reachable, but with nothing in it: the dashboard is up and
                        // no agent has ever published. Distinguishing this from
                        // "cannot reach the dashboard at all" is half the diagnosis,
                        // and it is the exact state this panel spent an hour in on
                        // 2026-09-22 while looking like nothing was wrong.
                        onStatus.accept("no_agent");
                    }
                }
                synchronized (this) {
                    wait(1000L);
                }
            } catch (InterruptedException ie) {
                return;
            } catch (Exception e) {
                // A machine-readable reason, so the overlay can say it in Chinese
                // (and in ASCII) rather than forwarding a Java class name to a
                // player. 404 is its own case: the port is answered by a server
                // that has no /state, which means an OLD dashboard is holding it —
                // the trap that cost an hour on 2026-09-22.
                String reason = "unreachable";
                if (e instanceof IllegalStateException
                        && String.valueOf(e.getMessage()).contains("404")) {
                    reason = "old_server";
                } else if (e instanceof java.net.ConnectException
                        || e instanceof java.net.SocketTimeoutException) {
                    reason = "unreachable";
                } else if (e instanceof java.net.UnknownHostException) {
                    reason = "unreachable";
                }
                onStatus.accept(reason);
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
        JSONObject advice = state.optJSONObject("last_advice");
        JSONObject outcome = state.optJSONObject("last_outcome");
        JSONObject last = state.optJSONObject("last_decision");
        JSONObject run = state.optJSONObject("run_state");
        JSONObject presence = state.optJSONObject("agent_state");
        if (advice == null && outcome == null && last == null && run == null
                && presence == null) {
            return null;
        }

        Snapshot snap = new Snapshot();

        if (presence != null) {
            snap.agentState = presence.optString("state", "");
            snap.agentDetail = presence.optString("detail", "");
        }

        if (advice != null) {
            String point = advice.optString("point", "");
            snap.pointLabel = pointZh(point);
            snap.pointAscii = point.isEmpty() ? "?" : point;
            snap.adviceLabel = advice.optString("label", "");
            snap.adviceAscii = asciiCommand(advice.optString("verb", ""),
                    advice.optJSONObject("command"));
            snap.adviceReason = advice.optString("reason", "");
            snap.adviceConfidence = advice.optDouble("confidence", 0.0);
            snap.ruleOnly = snap.adviceConfidence <= 0.0;
            snap.fallback = advice.optBoolean("fallback", false);
            snap.hasAdvice = !snap.adviceLabel.isEmpty() || !snap.adviceAscii.isEmpty();
        }

        if (outcome != null) {
            snap.verdict = outcome.optString("verdict", "");
            snap.actedLabel = outcome.optString("acted_label", "");
            JSONArray acted = outcome.optJSONArray("acted");
            snap.actedAscii = acted == null ? "" : asciiKey(acted);
            snap.tallyText = tallyText(outcome);
            snap.hasOutcome = !snap.verdict.isEmpty();
        }

        if (!snap.hasAdvice && last != null) {
            // Play mode (or a screen the router could not advise on): show the
            // decision headline exactly as this mod always did.
            String point = last.optString("point", "?");
            String value = String.valueOf(last.opt("value"));
            double conf = last.optDouble("confidence", 0.0);
            boolean fallback = last.optBoolean("fallback", false);
            snap.headline = String.format("[%s] -> %s   conf %.2f%s",
                    point, value, conf, fallback ? "  (rule took over)" : "");
            snap.detail = last.optString("reason", "");
            if (snap.detail.isEmpty()) {
                JSONObject gate = last.optJSONObject("gate");
                if (gate != null) {
                    snap.detail = gate.optString("reason", "");
                }
            }
        }

        if (run != null) {
            snap.hp = run.optInt("hp", 0);
            snap.maxHp = run.optInt("max_hp", 80);
            snap.budgetRemaining = run.optInt("budget_remaining", 0);
            int reserved = run.optInt("reserved", 0);
            snap.budgetMax = Math.max(1, snap.maxHp - reserved);
        } else {
            snap.maxHp = 80;
            snap.budgetMax = 80;
        }
        return snap;
    }

    /** "62%  采纳 8 · 未采纳 5 · 无法判定 3" — the coach's own report card. */
    private static String tallyText(JSONObject outcome) {
        Object agreement = outcome.opt("agreement");
        Object tally = outcome.opt("tally");
        if (agreement == null && !(tally instanceof JSONObject)) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        if (agreement instanceof Number) {
            sb.append(Math.round(((Number) agreement).doubleValue() * 100)).append('%');
        }
        if (tally instanceof JSONObject) {
            JSONObject t = (JSONObject) tally;
            sb.append("  ").append(t.optInt("match", 0)).append('/')
              .append(t.optInt("mismatch", 0)).append('/')
              .append(t.optInt("unobserved", 0));
        }
        return sb.toString().trim();
    }

    /** The point key in Chinese, plus nothing invented: unknown keys pass through. */
    private static String pointZh(String point) {
        if (point == null) {
            return "";
        }
        switch (point) {
            case "map":        return "地图选路";
            case "card_reward":return "卡牌奖励";
            case "event":      return "事件";
            case "rest":       return "篝火";
            case "shop":       return "商店";
            case "boss_relic": return "Boss遗物";
            case "combat":     return "战斗";
            case "combat_risk":return "战斗风险";
            case "grid":       return "选卡";
            case "navigation": return "过场";
            default:           return point;
        }
    }

    /** ASCII form of an action key, e.g. ["play","strike_r","cultist"]. */
    private static String asciiKey(JSONArray key) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < key.length(); i++) {
            String part = key.optString(i, "");
            if (part.isEmpty()) {
                continue;
            }
            sb.append(sb.length() == 0 ? "" : " ").append(part);
        }
        return sb.toString();
    }

    /**
     * ASCII form of a command, for installs whose font cannot draw the Chinese
     * label. Card numbers are 1-based here on purpose: this is the line a player
     * reads, and the game numbers cards 1..n (the wire is 1-based too, the
     * agent's own logs are 0-based, and mixing them up in the one place a human
     * reads would be worse than useless).
     */
    private static String asciiCommand(String verb, JSONObject command) {
        if (verb == null || verb.isEmpty()) {
            return "";
        }
        if (command == null) {
            return verb;
        }
        if ("play".equals(verb)) {
            int card = command.optInt("card", command.optInt("card_index", -1));
            Object target = command.opt("target");
            String s = "play card " + (card + 1);
            return target instanceof Number ? s + " -> enemy " + (((Number) target).intValue() + 1) : s;
        }
        if ("choose".equals(verb)) {
            Object name = command.opt("name");
            if (name instanceof String && !((String) name).isEmpty()) {
                return "choose " + name;
            }
            Object idx = command.opt("choice");
            return idx instanceof Number ? "choose " + idx : "choose";
        }
        return verb;
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
