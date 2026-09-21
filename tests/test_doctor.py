"""Tests for the environment doctor, mostly for the Steam ledger parser.

The parser is small but its failure mode is silent and misleading: a looser regex
also matches `"manifest" "4757618196275874364"` and the account id in
`"subscribedby"`, which then appear as phantom subscriptions. That happened the
first time this ran against a real `appworkshop_646570.acf`, so it is pinned here
with the real file's shape.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.doctor import (
    WORKSHOP_EXPECTED,
    read_workshop_ledger,
    workshop_content_dir,
)

# Real shape of appworkshop_646570.acf on this machine, 2026-09-21, with the
# account id and manifest numbers that the naive pattern mistook for item ids.
ACF = '''"AppWorkshop"
{
\t"appid"\t\t"646570"
\t"SizeOnDisk"\t\t"18596557"
\t"NeedsUpdate"\t\t"0"
\t"NeedsDownload"\t\t"1"
\t"WorkshopItemsInstalled"
\t{
\t\t"1605060445"
\t\t{
\t\t\t"size"\t\t"10252951"
\t\t\t"manifest"\t\t"4757618196275874364"
\t\t}
\t\t"1605833019"
\t\t{
\t\t\t"size"\t\t"7659408"
\t\t\t"manifest"\t\t"3847324195194341109"
\t\t}
\t}
\t"WorkshopItemDetails"
\t{
\t\t"1605060445"
\t\t{
\t\t\t"manifest"\t\t"4757618196275874364"
\t\t\t"subscribedby"\t\t"1747201501"
\t\t}
\t\t"1605833019"
\t\t{
\t\t\t"manifest"\t\t"3847324195194341109"
\t\t\t"subscribedby"\t\t"1747201501"
\t\t}
\t\t"2131373661"
\t\t{
\t\t\t"manifest"\t\t"5333477101442941486"
\t\t\t"timeupdated"\t\t"1642379493"
\t\t\t"subscribedby"\t\t"1747201501"
\t\t}
\t\t"3748153752"
\t\t{
\t\t\t"manifest"\t\t"1743123259934672883"
\t\t\t"timeupdated"\t\t"1782306334"
\t\t\t"subscribedby"\t\t"1747201501"
\t\t}
\t}
}
'''


def _fake_library(text: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    workshop = tmp / "steamapps" / "workshop"
    workshop.mkdir(parents=True)
    (workshop / "appworkshop_646570.acf").write_text(text, encoding="utf-8")
    content = workshop / "content" / "646570"
    for item in ("1605060445", "1605833019"):
        (content / item).mkdir(parents=True)
        (content / item / "Thing.jar").write_bytes(b"x")
    return tmp


def test_ledger_splits_subscribed_from_installed():
    lib = _fake_library(ACF)
    ledger = read_workshop_ledger(lib, "646570")
    assert ledger["exists"] is True
    assert ledger["installed"] == {"1605060445", "1605833019"}
    # subscribed but not downloaded is exactly the state we care about
    assert ledger["details"] == {"1605060445", "1605833019", "2131373661", "3748153752"}
    assert ledger["needs_download"] == "1"


def test_manifest_numbers_and_account_ids_are_not_mistaken_for_item_ids():
    lib = _fake_library(ACF)
    ledger = read_workshop_ledger(lib, "646570")
    noise = {"4757618196275874364", "3847324195194341109", "5333477101442941486",
             "1743123259934672883", "1747201501", "1642379493", "1782306334"}
    assert not (ledger["details"] & noise)
    assert not (ledger["installed"] & noise)


def test_missing_ledger_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = read_workshop_ledger(Path(tmp), "646570")
        assert ledger["exists"] is False
        assert ledger["installed"] == set() and ledger["details"] == set()
        assert ledger["needs_download"] is None


def test_content_dir_layout_matches_steam():
    with tempfile.TemporaryDirectory() as tmp:
        d = workshop_content_dir(Path(tmp), "646570")
        assert d == Path(tmp) / "steamapps" / "workshop" / "content" / "646570"


def test_the_expected_items_identify_communicationmod():
    """If this id ever changes, docs/SETUP.md and the download instructions with it."""
    name, required, why = WORKSHOP_EXPECTED["2131373661"]
    assert name == "CommunicationMod" and required is True and "pipe" in why


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
