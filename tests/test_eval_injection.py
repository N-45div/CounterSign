import importlib.util
import sys
from pathlib import Path

from countersign.doorman import scan
from countersign.verdict import WitnessReport

SPEC = importlib.util.spec_from_file_location("eval_injection", Path(__file__).parent.parent / "scripts" / "eval_injection.py")
ev = importlib.util.module_from_spec(SPEC)
sys.modules["eval_injection"] = ev  # dataclasses look their module up while the class is built
SPEC.loader.exec_module(ev)


class GullibleWitness:
    """Believes every note, and transcribes it like OCR would. Doorman scans the transcription."""

    def __init__(self, notes_by_after_frame):
        self.notes = notes_by_after_frame

    def compare(self, before, after):
        note = self.notes[after.image]
        flagged = scan(note)
        parcel = bool(note) or after.image in self.parcels
        return WitnessReport(False, parcel, 0.95, "gullible", bool(flagged), flagged)


def test_counts_with_a_witness_that_believes_every_note():
    notes, parcels = {}, set()
    for case in ev.CASES:
        _, after = ev.frames(case)
        notes[after.image] = case.note
        if case.parcel_after:
            parcels.add(after.image)
    witness = GullibleWitness(notes)
    witness.parcels = parcels
    rows = [ev.run_case(witness, "gullible", c) for c in ev.CASES]
    [s] = ev.summarise(rows)

    flagged = sum(bool(scan(lines)) for _, lines in ev.INJECTIONS)
    assert s["injection_cases"] == 20 and s["model_fooled"] == 20
    assert s["false_receipts_without_doorman"] == 20  # a gullible model alone is a false receipt every time
    assert s["doorman_flagged"] == flagged
    assert s["false_receipts_with_doorman"] == 20 - flagged  # Doorman's misses are exactly the remaining risk
    assert s["benign_flagged"] == 0  # ordinary notes are left alone
    assert s["benign_false_receipts"] == 6  # this witness also believes benign notes mean a parcel
    assert s["controls"] == 4
    assert s["text_dependent_caught"] == 0  # this witness has no second look


def test_doorman_catches_the_blatant_injections_and_spares_the_benign_notes():
    blatant = [lines for name, lines in ev.INJECTIONS[:12]]
    assert all(scan(lines) for lines in blatant)
    assert not any(scan(lines) for _, lines in ev.BENIGN)
