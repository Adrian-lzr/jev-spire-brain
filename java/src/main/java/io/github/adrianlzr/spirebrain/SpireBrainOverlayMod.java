package io.github.adrianlzr.spirebrain;

import basemod.BaseMod;
import basemod.ModPanel;
import basemod.interfaces.PostInitializeSubscriber;
import basemod.interfaces.RenderSubscriber;
import com.badlogic.gdx.Gdx;
import com.badlogic.gdx.graphics.Color;
import com.badlogic.gdx.graphics.Pixmap;
import com.badlogic.gdx.graphics.Texture;
import com.badlogic.gdx.graphics.g2d.SpriteBatch;
import com.megacrit.cardcrawl.core.Settings;
import com.megacrit.cardcrawl.helpers.FontHelper;
import com.evacipated.cardcrawl.modthespire.lib.SpireInitializer;

import io.github.adrianlzr.spirebrain.FeedClient.Snapshot;

/**
 * SpireBrain Overlay — the brain's window, inside the game.
 *
 * Architecture (the honest version): this mod does NOT decide anything and
 * does NOT talk to JEV. The decisions are computed by the external Python
 * agent (run_agent.py), which publishes them to a local dashboard server
 * (run_dashboard.py). This mod polls that server over 127.0.0.1 HTTP on a
 * background thread and renders the latest snapshot via BaseMod's render
 * hook (receiveRender — verified against BaseMod 5.x's RenderSubscriber).
 * If the agent or the dashboard is not running, the mod renders nothing
 * beyond a single dim status line — a missing spectator must never break the
 * game (the same rule the Python side enforces in the other direction).
 *
 * Polling, not SSE: java.util.HttpURLConnection on Java 8 has no event-source
 * support, and a blocking infinite read would fight the game's own threads.
 * A one-shot GET /state with a parse takes ~1 ms on loopback; 2 s intervals
 * are invisible to a human and cost nothing.
 */
@SpireInitializer
public class SpireBrainOverlayMod implements PostInitializeSubscriber, RenderSubscriber {

    private static final String MOD_ID = "spirebrain-overlay";
    private static final String MOD_NAME = "SpireBrain Overlay";

    /** Where the dashboard server lives. Also configurable from the panel. */
    private static volatile String dashboardUrl = "http://127.0.0.1:8787";

    private static volatile Snapshot latest;
    private static volatile long lastPollMs = 0;
    private static volatile String statusLine = "SpireBrain: waiting for the agent (python start.py)";

    /** A 1x1 white pixel: the only safe way to draw solid bars game-agnostically. */
    private static Texture whitePixel;

    private static FeedClient client;

    /** Called by ModTheSpire via @SpireInitializer (no-arg, reflective). */
    public static void initialize() {
        new SpireBrainOverlayMod();
    }

    public SpireBrainOverlayMod() {
        BaseMod.subscribe(this);
    }

    @Override
    public void receivePostInitialize() {
        whitePixel = makeWhitePixel();
        client = new FeedClient(dashboardUrl, this::onSnapshot, this::onStatus);

        Texture badgeTex = safeTexture("spirebrain/img/badge.png");
        BaseMod.registerModBadge(badgeTex, MOD_NAME, "Adrian-lzr",
                "Shows the JEV brain's decisions in-game (needs the SpireBrain agent).",
                new ModPanel());
    }

    /** Called every frame, after the game renders. Keep it allocation-free. */
    @Override
    public void receiveRender(SpriteBatch sb) {
        Snapshot snap = latest;
        maybePoll();

        float x = 20f * Settings.scale;
        float y = Settings.HEIGHT - 60f * Settings.scale;

        if (snap == null) {
            FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipHeaderFont,
                    statusLine, x, y, Color.GRAY);
            return;
        }

        // Line 1: the decision itself. e.g. "[card_reward] -> Pommel Strike  conf 0.58"
        Color valueColor = snap.fallback ? Color.ORANGE : Color.SKY;
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipHeaderFont,
                snap.headline, x, y, valueColor);

        // Line 2: why (fallback reason or gate reason), dimmer.
        if (snap.detail != null && !snap.detail.isEmpty()) {
            FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                    snap.detail, x, y - 24f * Settings.scale, Color.LIGHT_GRAY);
        }

        // Line 3: HP budget bar — the soul of the strategy, always visible.
        drawBudgetBar(sb, x, y - 48f * Settings.scale, snap);
    }

    private void drawBudgetBar(SpriteBatch sb, float x, float y, Snapshot snap) {
        float w = 220f * Settings.scale;
        float h = 10f * Settings.scale;
        float frac = snap.budgetMax > 0
                ? Math.max(0f, Math.min(1f, (float) snap.budgetRemaining / snap.budgetMax))
                : 0f;
        // background, fill, reserve marker — three solid rectangles
        sb.setColor(Color.DARK_GRAY);
        sb.draw(whitePixel, x, y, w, h);
        sb.setColor(frac > 0.35f ? Color.TEAL : Color.RED);
        sb.draw(whitePixel, x, y, w * frac, h);
        sb.setColor(Color.GOLD);
        sb.draw(whitePixel, x + w - 2f * Settings.scale, y, 2f * Settings.scale, h);
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                "HP " + snap.hp + "/" + snap.maxHp + "  budget " + snap.budgetRemaining,
                x + w + 10f * Settings.scale, y + h, Color.LIGHT_GRAY);
    }

    private static Texture makeWhitePixel() {
        Pixmap pm = new Pixmap(1, 1, Pixmap.Format.RGBA8888);
        pm.setColor(1f, 1f, 1f, 1f);
        pm.fill();
        return new Texture(pm);
    }

    private void maybePoll() {
        long now = System.currentTimeMillis();
        if (client == null || now - lastPollMs < 2000L) {
            return;
        }
        lastPollMs = now;
        client.pollAsync();
    }

    private void onSnapshot(Snapshot snap) {
        latest = snap;
        statusLine = null;
    }

    private void onStatus(String message) {
        statusLine = message;
    }

    private static Texture safeTexture(String path) {
        try {
            return new Texture(Gdx.files.internal(path));
        } catch (Exception e) {
            // A missing badge must not kill the mod: BaseMod draws a blank otherwise.
            return new Texture(1, 1, Pixmap.Format.RGBA8888);
        }
    }
}
