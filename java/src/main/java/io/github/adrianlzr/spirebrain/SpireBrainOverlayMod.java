package io.github.adrianlzr.spirebrain;

import basemod.BaseMod;
import basemod.ModPanel;
import basemod.interfaces.PostInitializeSubscriber;
import basemod.interfaces.RenderSubscriber;
import com.badlogic.gdx.Gdx;
import com.badlogic.gdx.Input;
import com.badlogic.gdx.graphics.Color;
import com.badlogic.gdx.graphics.Pixmap;
import com.badlogic.gdx.graphics.Texture;
import com.badlogic.gdx.graphics.g2d.BitmapFont;
import com.badlogic.gdx.graphics.g2d.SpriteBatch;
import com.megacrit.cardcrawl.core.Settings;
import com.megacrit.cardcrawl.helpers.FontHelper;
import com.evacipated.cardcrawl.modthespire.lib.SpireInitializer;

import io.github.adrianlzr.spirebrain.FeedClient.Snapshot;

/**
 * SpireBrain Overlay — the coach, inside the game.
 *
 * Architecture (the honest version): this mod does NOT decide anything and does
 * NOT talk to JEV. The decisions are computed by the external Python agent
 * (run_agent.py), which publishes them to a local dashboard server
 * (run_dashboard.py). This mod polls that server over 127.0.0.1 HTTP on a
 * background thread and renders the latest snapshot via BaseMod's render hook
 * (receiveRender — verified against BaseMod 5.x's RenderSubscriber). If the agent
 * or the dashboard is not running, the mod renders nothing beyond a single dim
 * status line — a missing spectator must never break the game (the same rule the
 * Python side enforces in the other direction).
 *
 * Polling, not SSE: java.util.HttpURLConnection on Java 8 has no event-source
 * support, and a blocking infinite read would fight the game's own threads.
 * A one-shot GET /state with a parse takes ~1 ms on loopback; 1 s intervals are
 * invisible to a human and cost nothing.
 *
 * WHAT IT SHOWS (rewritten 2026-09-22 evening). The player asked for the advice
 * in-game instead of in a browser tab they have to alt-tab to, and that is the
 * right ask: a coach you have to switch windows to read is not a coach. The panel
 * is now:
 *
 *     战斗                                  ← which decision point
 *     出「痛击」→ 咔咔                        ← the recommendation, large
 *     because: lethal this turn             ← the router's own reason
 *     你刚才: 出「打击」  没采纳                ← what you did, and whether it matched
 *     62%  8/5/3                            ← agreement rate and the tally
 *     [====HP====]  52/80  budget 28        ← the strategy's soul, unchanged
 *
 * TWO THINGS THAT COULD HAVE SILENTLY BROKEN IT, both handled here:
 *
 * 1. **Fonts.** Slay the Spire generates its CJK fonts at runtime from
 *    `font/FeDPrm27C.otf` (FreeType), so Chinese renders on a ZHS install — but
 *    only for glyphs the font actually has. Rather than hope, every Chinese
 *    string is checked with `BitmapFontData.hasGlyph` before it is drawn, and a
 *    string whose glyphs are missing falls back to its ASCII form. A missing
 *    glyph renders as nothing, and a panel that silently loses its one
 *    actionable line is worse than an English one.
 * 2. **Position.** The old overlay drew at the top-left, over the game's own HP
 *    bar and relic row. The panel is now centred at the top, over the one band
 *    that is empty on the map, in combat, and on reward screens.
 *
 * F8 hides and shows the panel — no config file, because a player whose screen
 * is covered needs one keypress, not a README.
 */
@SpireInitializer
public class SpireBrainOverlayMod implements PostInitializeSubscriber, RenderSubscriber {

    private static final String MOD_ID = "spirebrain-overlay";
    private static final String MOD_NAME = "SpireBrain Overlay";

    /** Where the dashboard server lives. */
    private static volatile String dashboardUrl = "http://127.0.0.1:8787";

    private static volatile Snapshot latest;
    private static volatile long lastPollMs = 0;
    /** Why there is nothing to show yet: a code from the poller, not prose. */
    private static volatile String statusCode = "idle";
    private static volatile boolean visible = true;

    /** A 1x1 white pixel: the only safe way to draw solid bars game-agnostically. */
    private static Texture whitePixel;

    private static FeedClient client;

    /** Display strings resolved against the game's fonts, cached per snapshot. */
    private static Snapshot resolvedFor;
    private static String rPoint, rAdvice, rReason, rActed, rVerdict, rTally;
    private static Color rAdviceColor, rVerdictColor;

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
                "Shows the JEV brain's recommendation, and what you did with it, "
                + "in-game. Needs the SpireBrain agent (python start.py). "
                + "F8 hides/shows the panel.", new ModPanel());
    }

    /** Called every frame, after the game renders. Keep it allocation-light. */
    @Override
    public void receiveRender(SpriteBatch sb) {
        Snapshot snap = latest;
        maybePoll();

        if (Gdx.input.isKeyJustPressed(Input.Keys.F8)) {
            visible = !visible;
        }
        if (!visible) {
            return;
        }

        if (snap == null) {
            // NOT the silent path any more. On 2026-09-22 this panel drew a dim
            // grey line at the top-left when it had no data, and the player
            // reasonably reported "the panel never appears" — an hour went into
            // finding an empty `command=` in one of the two CommunicationMod
            // configs. A surface that shows nothing must at least say that it has
            // nothing, and why.
            drawNotice(sb);
            return;
        }
        if (!snap.hasAdvice && !snap.hasOutcome && snap.headline.isEmpty()
                && "waiting_for_run".equals(snap.agentState)) {
            // The agent is ALIVE and waiting for the player to start a run.
            // Rendering "agent not online" here (the 20:50 misreport) taught the
            // player their setup was broken when it was working exactly as
            // designed. A calm green note, same panel shape.
            drawWaiting(sb, snap);
            return;
        }
        resolveStrings(snap);
        drawPanel(sb, snap);
    }

    /** The advisor is alive and waiting for the player's first decision. */
    private void drawWaiting(SpriteBatch sb, Snapshot snap) {
        float scale = Settings.scale;
        float pad = 14f * scale;
        float w = Math.min(Settings.WIDTH * 0.52f, 760f * scale);
        float x = (Settings.WIDTH - w) * 0.5f;
        float top = Settings.HEIGHT - 26f * scale;
        float bottom = top - 96f * scale;

        sb.setColor(0f, 0f, 0f, 0.62f);
        sb.draw(whitePixel, x - pad, bottom, w + 2f * pad, top - bottom + pad);

        BitmapFont big = bigFont();
        if (big == null) {
            return;
        }
        Color calm = new Color(0.55f, 0.85f, 0.6f, 1f);
        FontHelper.renderFontLeftTopAligned(sb, big,
                fit(big, "军师在线，等你开局", "SpireBrain: online, waiting for you"),
                x, top - 8f * scale, calm);
        String detail = snap.agentDetail == null || snap.agentDetail.isEmpty()
                ? "开局后第一屏开始给建议" : snap.agentDetail;
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                fit(FontHelper.tipBodyFont, detail,
                        "advice starts on the run's first screen"),
                x, top - 44f * scale, Color.LIGHT_GRAY);
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                "F8 = hide   |   " + dashboardUrl, x, top - 68f * scale, Color.GRAY);
    }

    /**
     * The "no data yet" panel: same backdrop, same position, one clear sentence.
     *
     * Deliberately the same shape as the real panel rather than a different
     * widget: the player learns one place to look, and "nothing there" is
     * replaced by "here is what is wrong" instead of by nothing at all.
     */
    private void drawNotice(SpriteBatch sb) {
        float scale = Settings.scale;
        float pad = 14f * scale;
        float w = Math.min(Settings.WIDTH * 0.52f, 760f * scale);
        float x = (Settings.WIDTH - w) * 0.5f;
        float top = Settings.HEIGHT - 26f * scale;
        float bottom = top - 96f * scale;

        sb.setColor(0f, 0f, 0f, 0.62f);
        sb.draw(whitePixel, x - pad, bottom, w + 2f * pad, top - bottom + pad);

        String code = statusCode;
        String zh = "军师未连接";
        String ascii = "SpireBrain: not connected";
        String hintZh;
        String hintAscii;
        switch (code == null ? "idle" : code) {
            case "no_agent":
                zh = "军师待命：agent 还没上线";
                ascii = "SpireBrain: waiting for the agent";
                hintZh = "游戏外的 agent 由 CommunicationMod 启动，检查它的配置里 command= 不为空";
                hintAscii = "the agent is launched by CommunicationMod; check its config's command=";
                break;
            case "old_server":
                zh = "端口上是一个旧版 dashboard（没有 /state）";
                ascii = "an OLD dashboard is holding this port (no /state)";
                hintZh = "关掉它再重启 start.py，或用 --port 换端口";
                hintAscii = "close it and re-run start.py (or use --port)";
                break;
            case "unreachable":
                zh = "连不上 dashboard：start.py 好像没在运行";
                ascii = "cannot reach the dashboard: is start.py running?";
                hintZh = "双击 日常启动.bat（或 python start.py），然后进游戏";
                hintAscii = "run start.py, then start the game";
                break;
            default:
                hintZh = "先运行 start.py，再开始一局";
                hintAscii = "run start.py, then start a run";
                break;
        }

        BitmapFont big = bigFont();
        if (big == null) {
            return;   // nothing to draw with; the game is still starting up
        }
        FontHelper.renderFontLeftTopAligned(sb, big,
                fit(big, zh, ascii), x, top - 8f * scale, Color.ORANGE);
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                fit(FontHelper.tipBodyFont, hintZh, hintAscii),
                x, top - 44f * scale, Color.LIGHT_GRAY);
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                "F8 = hide   |   " + dashboardUrl, x, top - 68f * scale, Color.GRAY);
    }

    // --------------------------------------------------------------------- //
    // Layout
    // --------------------------------------------------------------------- //
    private void drawPanel(SpriteBatch sb, Snapshot snap) {
        float scale = Settings.scale;
        float pad = 14f * scale;
        float w = Math.min(Settings.WIDTH * 0.52f, 760f * scale);
        float x = (Settings.WIDTH - w) * 0.5f;
        float top = Settings.HEIGHT - 26f * scale;

        float yPoint = top - 6f * scale;
        float yAdvice = yPoint - 30f * scale;
        float yReason = yAdvice - 30f * scale;
        float yActed = yReason - 34f * scale;
        float yTally = yActed - 26f * scale;
        float yBar = yTally - 26f * scale;
        float bottom = yBar - 18f * scale;

        // A translucent backdrop. The game's art is busy and a coach that cannot
        // be read mid-fight is not a coach; 0.62 alpha keeps the scene visible.
        sb.setColor(0f, 0f, 0f, 0.62f);
        sb.draw(whitePixel, x - pad, bottom, w + 2f * pad, top - bottom + pad);

        // Which decision point, small and dim.
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont, rPoint,
                x, yPoint, Color.LIGHT_GRAY);

        if (snap.hasAdvice) {
            FontHelper.renderFontLeftTopAligned(sb, bigFont(), rAdvice, x, yAdvice,
                    rAdviceColor);
            if (!rReason.isEmpty()) {
                FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont, rReason,
                        x, yReason, Color.LIGHT_GRAY);
            }
        } else if (!snap.headline.isEmpty()) {
            // Play mode: the decision is the content.
            FontHelper.renderFontLeftTopAligned(sb, bigFont(), snap.headline, x, yAdvice,
                    snap.fallback ? Color.ORANGE : Color.SKY);
            if (!snap.detail.isEmpty()) {
                FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont, snap.detail,
                        x, yReason, Color.LIGHT_GRAY);
            }
        }

        if (snap.hasOutcome) {
            FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipHeaderFont, rActed,
                    x, yActed, Color.WHITE);
            if (!rVerdict.isEmpty()) {
                // Right-aligned judgement, so the two facts never overlap.
                float vw = FontHelper.getWidth(FontHelper.tipHeaderFont, rVerdict, scale);
                FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipHeaderFont, rVerdict,
                        x + w - vw, yActed, rVerdictColor);
            }
        }
        if (!rTally.isEmpty()) {
            FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont, rTally,
                    x, yTally, Color.GRAY);
        }
        drawBudgetBar(sb, x, yBar, w, snap);
    }

    private void drawBudgetBar(SpriteBatch sb, float x, float y, float w, Snapshot snap) {
        float h = 10f * Settings.scale;
        float frac = snap.budgetMax > 0
                ? Math.max(0f, Math.min(1f, (float) snap.budgetRemaining / snap.budgetMax))
                : 0f;
        sb.setColor(Color.DARK_GRAY);
        sb.draw(whitePixel, x, y, w, h);
        sb.setColor(frac > 0.35f ? Color.TEAL : Color.RED);
        sb.draw(whitePixel, x, y, w * frac, h);
        sb.setColor(Color.GOLD);
        sb.draw(whitePixel, x + w - 2f * Settings.scale, y, 2f * Settings.scale, h);
        FontHelper.renderFontLeftTopAligned(sb, FontHelper.tipBodyFont,
                "HP " + snap.hp + "/" + snap.maxHp + "   budget " + snap.budgetRemaining,
                x + w + 10f * Settings.scale, y + h, Color.LIGHT_GRAY);
    }

    // --------------------------------------------------------------------- //
    // Strings, resolved once per snapshot
    // --------------------------------------------------------------------- //
    /**
     * Pick the drawable form of every string, once per new snapshot.
     *
     * Done here rather than inside the render loop for two reasons: the glyph
     * probe is not free, and doing it per frame would allocate ~6 strings 60
     * times a second on the game's own thread.
     */
    private static void resolveStrings(Snapshot snap) {
        if (snap == resolvedFor) {
            return;
        }
        resolvedFor = snap;
        BitmapFont big = bigFont();

        rPoint = fit(FontHelper.tipBodyFont, snap.pointLabel, snap.pointAscii);
        rAdvice = fit(big, snap.adviceLabel, snap.adviceAscii);
        rReason = fit(FontHelper.tipBodyFont, snap.adviceReason, "");
        rActed = fit(FontHelper.tipHeaderFont, "你刚才: " + snap.actedLabel,
                snap.actedAscii.isEmpty() ? "" : "you: " + snap.actedAscii);
        rVerdict = verdictText(snap.verdict);
        rTally = fit(FontHelper.tipBodyFont, verdictPrefix(snap) + snap.tallyText,
                snap.tallyText);

        // Orange has meant "a rule answered, not the model" in this mod since the
        // first version; the advisor keeps that meaning for the recommendation so
        // a player learns one colour vocabulary, not two.
        rAdviceColor = (snap.ruleOnly || snap.fallback) ? Color.ORANGE : Color.SKY;
        rVerdictColor = "match".equals(snap.verdict) ? Color.TEAL
                : "mismatch".equals(snap.verdict) ? Color.ORANGE : Color.GRAY;
    }

    /** The verdict word, Chinese if the font can, ASCII if it cannot. */
    private static String verdictText(String verdict) {
        if (verdict == null || verdict.isEmpty()) {
            return "";
        }
        switch (verdict) {
            case "match":
                return fit(FontHelper.tipHeaderFont, "采纳", "MATCH");
            case "mismatch":
                return fit(FontHelper.tipHeaderFont, "没采纳", "DIFFERENT");
            case "unobserved":
                return fit(FontHelper.tipHeaderFont, "没看出来", "UNCLEAR");
            default:
                return verdict;
        }
    }

    /** "命中率 " only when the font can draw it; the digits speak for themselves. */
    private static String verdictPrefix(Snapshot snap) {
        if (!snap.tallyText.contains("%")) {
            return "";
        }
        return fit(FontHelper.tipBodyFont, "命中率 ", "agreement ");
    }

    /**
     * `chinese` if the font has every glyph it needs, else `ascii`.
     *
     * This is the guard against the one silent failure that matters here: a
     * BitmapFont renders a missing glyph as *nothing*, so a font without 采纳
     * would leave the verdict blank rather than wrong — and a blank verdict looks
     * like a bug in the agent, not in the font.
     */
    private static String fit(BitmapFont font, String chinese, String ascii) {
        if (chinese == null || chinese.isEmpty()) {
            return ascii == null ? "" : ascii;
        }
        return canRender(font, chinese) ? chinese : (ascii == null ? "" : ascii);
    }

    private static boolean canRender(BitmapFont font, String text) {
        if (font == null || font.getData() == null) {
            return false;
        }
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == ' ' || c == '\n') {
                continue;   // every font in the game can draw these
            }
            try {
                if (!font.getData().hasGlyph(c)) {
                    return false;
                }
            } catch (Exception e) {
                return false;   // a font we cannot interrogate is a font we cannot trust
            }
        }
        return true;
    }

    /**
     * The biggest font available that can draw the text.
     *
     * `cardTitleFont` is the largest font the game exposes on every install; the
     * fallback chain exists because a mod loading before FontHelper finishes
     * would otherwise NPE in receiveRender, which crashes the game rather than
     * hiding a panel.
     */
    private static BitmapFont bigFont() {
        BitmapFont f = FontHelper.cardTitleFont;
        if (f == null) {
            f = FontHelper.tipHeaderFont;
        }
        if (f == null) {
            f = FontHelper.tipBodyFont;
        }
        return f;
    }

    private static Texture makeWhitePixel() {
        Pixmap pm = new Pixmap(1, 1, Pixmap.Format.RGBA8888);
        pm.setColor(1f, 1f, 1f, 1f);
        pm.fill();
        return new Texture(pm);
    }

    private void maybePoll() {
        long now = System.currentTimeMillis();
        if (client == null || now - lastPollMs < 1000L) {
            return;
        }
        lastPollMs = now;
        client.pollAsync();
    }

    private void onSnapshot(Snapshot snap) {
        latest = snap;
        statusCode = null;
    }

    private void onStatus(String code) {
        // Do NOT clear `latest` here: a snapshot that arrived earlier is still the
        // best thing we know, and blanking the panel on one failed poll would make
        // a flaky second look like a dead agent.
        statusCode = code;
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
