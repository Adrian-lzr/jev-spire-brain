package io.github.adrianlzr.spirebrain;

/**
 * Runs the mod's own poller and parser against a live dashboard, printing exactly
 * what the panel would show — the part of the overlay that cannot be checked by
 * reading the code.
 *
 * Why this exists: `FeedClient` is the only piece of the mod with real logic
 * (HTTP, JSON, the Chinese/ASCII pairing). Everything else is drawing calls, and
 * the drawing calls can only be judged in the game. Without this tool the first
 * test of the parser would be a player looking at a wrong panel and having to
 * guess whether the agent, the JSON, or the parse was at fault.
 *
 *   java -cp SpireBrainOverlay.jar io.github.adrianlzr.spirebrain.ParseCheck [url] [seconds]
 *
 * It needs no game classes: org.json is shaded into the jar, and this file
 * deliberately touches nothing from com.megacrit.*.
 */
public final class ParseCheck {

    public static void main(String[] args) throws Exception {
        String url = args.length > 0 ? args[0] : "http://127.0.0.1:8787";
        long seconds = args.length > 1 ? Long.parseLong(args[1]) : 6L;

        System.out.println("polling " + url + "/state for " + seconds + "s");
        FeedClient client = new FeedClient(url,
                snapshot -> {
                    System.out.println("--- snapshot ---");
                    if (snapshot.hasAdvice) {
                        System.out.println("  point      : " + snapshot.pointLabel
                                + "   (ascii: " + snapshot.pointAscii + ")");
                        System.out.println("  advice ZH  : " + snapshot.adviceLabel);
                        System.out.println("  advice ASC : " + snapshot.adviceAscii);
                        System.out.println("  reason     : " + snapshot.adviceReason);
                        System.out.println("  confidence : " + snapshot.adviceConfidence
                                + (snapshot.ruleOnly ? "  (rules, model not asked)" : ""));
                    } else {
                        System.out.println("  advice     : (none) headline=" + snapshot.headline);
                    }
                    if (snapshot.hasOutcome) {
                        System.out.println("  verdict    : " + snapshot.verdict
                                + "   acted ZH: " + snapshot.actedLabel
                                + "   acted ASC: " + snapshot.actedAscii);
                        System.out.println("  tally      : " + snapshot.tallyText);
                    } else {
                        System.out.println("  verdict    : (nothing judged yet)");
                    }
                    System.out.println("  run        : HP " + snapshot.hp + "/" + snapshot.maxHp
                            + "  budget " + snapshot.budgetRemaining + "/" + snapshot.budgetMax);
                },
                message -> System.out.println("[status] " + message));

        Thread.sleep(seconds * 1000L);
        client.pollAsync();
        Thread.sleep(1500L);
        System.out.println("done");
    }
}
