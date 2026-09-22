"""The overlay layer: watch the brain think while it plays.

Phase 1.5 of jev-spire-brain. The brain itself is untouched — this package
*observes* it. `feed.py` holds the event stream, `server.py` serves a local
dashboard over SSE, `demo.py` drives the real decision modules with the mock
client so the whole loop can be shown with no game and no API key.
"""
