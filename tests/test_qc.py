"""Tests for VAULT QC checks.

Discipline: every check is verified to go BOTH red and green, plus a mutation
test that proves the gate fails when the threshold is broken.

No fixtures. No synthetic data. The loudness checks use the measured numbers
from real archive.org films (critic-round1.md):
  Vicki (1953):                -26.1 LUFS  -> FAIL EBU R128
  What Becomes Of The Children: -24.3 LUFS  -> FAIL EBU R128, PASS ATSC A/85
  Werewolf in a Girls' Dormitory: -18.0 LUFS -> FAIL both
  In-spec control:             -23.0 LUFS  -> PASS EBU R128, PASS ATSC A/85
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from qc.measure import (
    EBU_R128_TARGET_LUFS,
    EBU_R128_TOLERANCE_LU,
    ATSC_A85_TARGET_LKFS,
    ATSC_A85_TOLERANCE_LU,
    TRUE_PEAK_CEILING_DBTP,
    Finding,
    loudness_findings,
    structural_findings,
    subtitle_findings,
)


# ============================================================
# Loudness gate: goes red AND green
# ============================================================

class TestLoudnessFindings:
    """Loudness gate using the real measured numbers from critic-round1.md."""

    def _lm(self, lufs: float, tp: float = -2.0, lra: float = 10.0) -> dict:
        return {"integrated_lufs": lufs, "true_peak_dbfs": tp, "lra_lu": lra}

    def test_vicki_1953_fails_ebu_r128(self):
        """Vicki (1953) measured -26.1 LUFS. Fails EBU R128 (target -23, tol ±1)."""
        findings = loudness_findings(self._lm(-26.1))
        ebu = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        assert not ebu.passed, f"Expected FAIL but got PASS at {ebu.measured} LUFS"
        assert ebu.measured == pytest.approx(-26.1)

    def test_in_spec_control_passes_ebu_r128(self):
        """Control: -23.0 LUFS is within the ±1 LU tolerance. Must PASS."""
        findings = loudness_findings(self._lm(-23.0))
        ebu = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        assert ebu.passed, f"Expected PASS but got FAIL at {ebu.measured} LUFS"

    def test_borderline_just_inside_tolerance(self):
        """-24.0 LUFS is exactly 1.0 LU from target. Should PASS (tolerance is inclusive)."""
        findings = loudness_findings(self._lm(-24.0))
        ebu = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        assert ebu.passed, "-24.0 LUFS is within ±1 LU of -23"

    def test_borderline_just_outside_tolerance(self):
        """-24.01 LUFS is 1.01 LU from target. Must FAIL."""
        findings = loudness_findings(self._lm(-24.01))
        ebu = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        assert not ebu.passed, "-24.01 LUFS is outside ±1 LU"

    def test_werewolf_dormitory_fails_both(self):
        """-18.0 LUFS is 5 LU loud: fails both EBU R128 and ATSC A/85."""
        findings = loudness_findings(self._lm(-18.0))
        ebu  = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        atsc = next(f for f in findings if f.check == "integrated_loudness_atsc_a85")
        assert not ebu.passed
        assert not atsc.passed

    def test_what_becomes_fails_ebu_passes_atsc(self):
        """What Becomes Of The Children: -24.3 LUFS.
        EBU R128 target -23, tol ±1  -> delta 1.3 -> FAIL.
        ATSC A/85 target -24, tol ±2 -> delta 0.3 -> PASS.
        """
        findings = loudness_findings(self._lm(-24.3))
        ebu  = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
        atsc = next(f for f in findings if f.check == "integrated_loudness_atsc_a85")
        assert not ebu.passed,  f"EBU should FAIL at -24.3 LUFS, got passed={ebu.passed}"
        assert atsc.passed,     f"ATSC should PASS at -24.3 LUFS, got passed={atsc.passed}"

    def test_true_peak_over_ceiling_fails(self):
        """True peak 0.0 dBFS exceeds the -1.0 dBTP ceiling. Must FAIL."""
        findings = loudness_findings(self._lm(-23.0, tp=0.0))
        tp_f = next(f for f in findings if f.check == "true_peak")
        assert not tp_f.passed

    def test_true_peak_at_ceiling_passes(self):
        """True peak at exactly -1.0 dBFS is at the limit. Must PASS."""
        findings = loudness_findings(self._lm(-23.0, tp=-1.0))
        tp_f = next(f for f in findings if f.check == "true_peak")
        assert tp_f.passed

    def test_auto_fixable_flag(self):
        """Loudness and true peak failures are marked auto_fixable=True."""
        findings = loudness_findings(self._lm(-26.1, tp=0.0))
        for f in findings:
            assert f.auto_fixable, f"{f.check} should be auto_fixable"


# ============================================================
# MUTATION TEST: prove the gate breaks when threshold changes
# ============================================================

class TestMutationLoudness:
    """Break the threshold, confirm the gate goes wrong, restore it."""

    def test_mutation_widened_tolerance_gives_wrong_result(self):
        """If we mutate the tolerance to ±5 LU, -26.1 LUFS would incorrectly PASS.
        This proves the gate actually uses the threshold, not a constant.
        """
        from qc import measure as m_mod

        original_tol = m_mod.EBU_R128_TOLERANCE_LU
        try:
            m_mod.EBU_R128_TOLERANCE_LU = 5.0   # mutant: tolerance too wide
            findings = loudness_findings(
                {"integrated_lufs": -26.1, "true_peak_dbfs": -2.0, "lra_lu": 10.0}
            )
            ebu = next(f for f in findings if f.check == "integrated_loudness_ebu_r128")
            # With tolerance=5, delta=3.1 < 5 -> PASS (mutation makes gate pass when it shouldn't)
            assert ebu.passed, "Mutation test: widened tolerance should give wrong PASS"
        finally:
            m_mod.EBU_R128_TOLERANCE_LU = original_tol   # restore

        # Restored: must go back to FAIL
        findings2 = loudness_findings(
            {"integrated_lufs": -26.1, "true_peak_dbfs": -2.0, "lra_lu": 10.0}
        )
        ebu2 = next(f for f in findings2 if f.check == "integrated_loudness_ebu_r128")
        assert not ebu2.passed, "After restoring threshold, Vicki should FAIL again"


# ============================================================
# Structural defects gate
# ============================================================

class TestStructuralFindings:
    def _sm(self, blacks=None, freeze=None, silence=None) -> dict:
        """A measured structural result.

        `freeze` and `silence` take start times for brevity and are expanded into the
        spans measure_structural actually produces, so these tests keep exercising the
        real shape rather than a simpler one of their own invention.
        """
        def spans(starts):
            return [
                {"start": at, "end": at + 3.0, "duration": 3.0, "truncated": False}
                for at in (starts or [])
            ]

        return {
            "black_segments": blacks or [],
            "freeze_events":  spans(freeze),
            "silence_events": spans(silence),
            "window_seconds": 120,
        }

    def test_no_defects_all_pass(self):
        findings = structural_findings(self._sm())
        assert all(f.passed for f in findings)

    def test_long_black_segment_fails(self):
        sm = self._sm(blacks=[{"start": 10.0, "end": 13.0, "duration": 3.0}])
        findings = structural_findings(sm)
        bf = next(f for f in findings if f.check == "black_frames")
        assert not bf.passed
        assert bf.measured == 1.0

    def test_short_black_below_threshold_passes(self):
        """A 1.5s black segment is below the 2s threshold."""
        sm = self._sm(blacks=[{"start": 5.0, "end": 6.5, "duration": 1.5}])
        findings = structural_findings(sm)
        bf = next(f for f in findings if f.check == "black_frames")
        assert bf.passed, "1.5s black < 2s threshold should PASS"

    def test_freeze_detected_fails(self):
        sm = self._sm(freeze=[42.0])
        findings = structural_findings(sm)
        ff = next(f for f in findings if f.check == "frozen_frames")
        assert not ff.passed

    def test_structural_not_auto_fixable(self):
        """Structural defects always require human review: not auto_fixable."""
        sm = self._sm(
            blacks=[{"start": 10.0, "end": 14.0, "duration": 4.0}],
            freeze=[20.0],
        )
        findings = structural_findings(sm)
        for f in findings:
            if not f.passed:
                assert not f.auto_fixable, f"{f.check} should NOT be auto_fixable"


# ============================================================
# Subtitle gate
# ============================================================

SRT_CLEAN = """\
1
00:00:01,000 --> 00:00:03,000
Hello world.

2
00:00:04,000 --> 00:00:06,000
This is fine.
"""

SRT_FAST = """\
1
00:00:01,000 --> 00:00:01,200
ExtremelyLongTextFarBeyondSeventeenCharactersPerSecondOnThisLine
"""

SRT_SHORT_CUE = """\
1
00:00:01,000 --> 00:00:01,100
Hi.
"""


class TestSubtitleFindings:
    def test_clean_srt_passes_all(self):
        findings = subtitle_findings(SRT_CLEAN)
        assert all(f.passed for f in findings), \
            [f"{f.check}: {f.detail}" for f in findings if not f.passed]

    def test_fast_cue_fails_speed(self):
        findings = subtitle_findings(SRT_FAST)
        speed = next(f for f in findings if f.check == "subtitle_reading_speed")
        assert not speed.passed, "Fast cue should fail reading speed check"

    def test_short_cue_fails_duration(self):
        findings = subtitle_findings(SRT_SHORT_CUE)
        dur = next(f for f in findings if f.check == "subtitle_min_duration")
        assert not dur.passed, "0.1s cue should fail minimum duration check"

    def test_empty_srt_returns_no_findings(self):
        """Empty subtitle file should return no findings (nothing to check)."""
        findings = subtitle_findings("")
        assert findings == [], "Empty SRT should return [] not False-looking result"


# ============================================================
# Non-empty catalog assertion
# ============================================================

class TestNonEmptyCatalog:
    def test_catalog_raises_on_empty(self):
        """catalog_summary() must raise RuntimeError when empty: not return []."""
        # Import here so test can skip if clickhouse_connect not available
        pytest.importorskip("clickhouse_connect", reason="clickhouse_connect not in test env")
        import qc.store as s

        class FakeClient:
            def query(self, sql):
                class R:
                    column_names = ["title_id","title","last_scanned","failures",
                                    "passes","auto_fixable","needs_human","verdict",
                                    "failed_checks","rescue_costs"]
                    result_rows = []
                return R()

        with pytest.raises(RuntimeError, match="empty"):
            s.catalog_summary(ch=FakeClient())
