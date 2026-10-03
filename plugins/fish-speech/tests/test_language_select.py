"""Pure-function tests for the language voice selector (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from language_voice import RUSSIAN_REFERENCE_ID, select_voice

GENERAL = "933563129e564b19a115bedd57b7406a"


def _payload(choice, confidence, probs):
    return {"answers": {"language": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": probs}}}


def test_strong_russian_selects_russian_voice():
    ref, lang = select_voice(
        _payload("russian", 0.99,
                 {"english": 0.01, "russian": 0.98, "other": 0.01}), GENERAL)
    assert ref == RUSSIAN_REFERENCE_ID
    assert lang == "russian"


def test_strong_english_keeps_general_voice():
    ref, lang = select_voice(
        _payload("english", 0.99,
                 {"english": 0.98, "russian": 0.01, "other": 0.01}), GENERAL)
    assert ref == GENERAL
    assert lang == "english"


def test_low_confidence_abstains_to_other():
    ref, lang = select_voice(
        _payload("russian", 0.50,
                 {"english": 0.01, "russian": 0.98, "other": 0.01}), GENERAL)
    assert ref == GENERAL
    assert lang == "other"


def test_malformed_payload_abstains():
    ref, lang = select_voice({"answers": {}}, GENERAL)
    assert ref == GENERAL
    assert lang == "unknown"
