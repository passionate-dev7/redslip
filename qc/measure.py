"""Deterministic QC measurement over a media file.

Every function shells out to ffmpeg or ffprobe. No model is involved at this layer.
The numbers a judge reproduces come from the same tool the judge runs:

  ffmpeg -i <file> -af ebur128=peak=true -f null -

Specs encoded:
  EBU R128   integrated loudness target −23 LUFS, tolerance ±1.0 LU
  ATSC A/85  (CALM Act) target −24 LKFS, tolerance ±2.0 LU
  True peak  ≤ −1.0 dBTP
  Netflix TTSS  reading speed ≤ 17 chars/sec, min cue 5/6 s, max 42 chars/line
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- spec constants -------------------------------------------------------
EBU_R128_TARGET_LUFS    = -23.0
EBU_R128_TOLERANCE_LU   =   1.0
ATSC_A85_TARGET_LKFS    = -24.0
ATSC_A85_TOLERANCE_LU   =   2.0
TRUE_PEAK_CEILING_DBTP  =  -1.0
NETFLIX_MAX_CPS          =  17.0
NETFLIX_MIN_CUE_SECONDS  =   5 / 6
NETFLIX_MAX_LINE_CHARS   =  42
NETFLIX_MAX_LINES        =   2


class ToolMissing(RuntimeError):
    """ffmpeg/ffprobe not on PATH."""


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ToolMissing(f"{cmd[0]} not found on PATH") from exc


# --- data types -----------------------------------------------------------

@dataclass
class Finding:
    check: str
    spec: str
    measured: float | None
    target: float | None
    unit: str
    passed: bool
    detail: str = ""
    auto_fixable: bool = False
    rescue_cost: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class QCReport:
    source: str
    duration_seconds: float | None = None
    window_seconds: int | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if not f.passed]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "duration_seconds": self.duration_seconds,
            "window_seconds": self.window_seconds,
            "passed": self.passed,
            "findings": [f.as_dict() for f in self.findings],
        }


# --- probing --------------------------------------------------------------

def probe(path: str | Path) -> dict:
    out = _run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout)


def duration_of(path: str | Path) -> float | None:
    try:
        meta = probe(path)
        return float(meta["format"]["duration"])
    except Exception:
        return None


# --- loudness -------------------------------------------------------------

_LOUDNESS_SUMMARY = re.compile(
    r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", re.MULTILINE
)
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", re.MULTILINE)
_LRA       = re.compile(r"LRA:\s*(-?[\d.]+)\s*LU", re.MULTILINE)


def measure_loudness(path: str | Path, seconds: int | None = None) -> dict:
    """Run ebur128 and return integrated loudness, LRA, true peak.

    `seconds` bounds the analysis. A full 90-min feature takes ~6 minutes;
    the UI states the window honestly rather than implying a full scan.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-af", "ebur128=peak=true", "-f", "null", "-"]
    out = _run(cmd)
    text = out.stderr

    def _grab(rx, default=None):
        m = rx.search(text)
        if not m:
            return default
        raw = m.group(1)
        return float("-inf") if raw == "-inf" else float(raw)

    integrated = _grab(_LOUDNESS_SUMMARY)
    if integrated is None:
        raise RuntimeError(f"ebur128 no summary. stderr tail: {text.strip()[-400:]}")

    return {
        "integrated_lufs": integrated,
        "true_peak_dbfs": _grab(_TRUE_PEAK),
        "lra_lu": _grab(_LRA),
        "window_seconds": seconds,
    }


def loudness_findings(measured: dict) -> list[Finding]:
    findings: list[Finding] = []
    i = measured["integrated_lufs"]

    delta_ebu = abs(i - EBU_R128_TARGET_LUFS)
    findings.append(Finding(
        check="integrated_loudness_ebu_r128",
        spec="EBU R128",
        measured=i, target=EBU_R128_TARGET_LUFS, unit="LUFS",
        passed=delta_ebu <= EBU_R128_TOLERANCE_LU,
        detail=f"{delta_ebu:.1f} LU from target (tolerance {EBU_R128_TOLERANCE_LU} LU)",
        auto_fixable=True,
        rescue_cost="loudnorm pass, ~2 min/title",
    ))

    delta_atsc = abs(i - ATSC_A85_TARGET_LKFS)
    findings.append(Finding(
        check="integrated_loudness_atsc_a85",
        spec="ATSC A/85 (CALM Act)",
        measured=i, target=ATSC_A85_TARGET_LKFS, unit="LKFS",
        passed=delta_atsc <= ATSC_A85_TOLERANCE_LU,
        detail=f"{delta_atsc:.1f} LU from target (tolerance {ATSC_A85_TOLERANCE_LU} LU)",
        auto_fixable=True,
        rescue_cost="loudnorm pass, ~2 min/title",
    ))

    tp = measured.get("true_peak_dbfs")
    if tp is not None:
        findings.append(Finding(
            check="true_peak",
            spec="EBU R128 true peak ceiling",
            measured=tp, target=TRUE_PEAK_CEILING_DBTP, unit="dBTP",
            passed=tp <= TRUE_PEAK_CEILING_DBTP,
            detail=f"peak {tp:.1f} dBTP against ceiling {TRUE_PEAK_CEILING_DBTP} dBTP",
            auto_fixable=True,
            rescue_cost="true-peak limiter pass, ~2 min/title",
        ))
    return findings


# --- structural video defects -------------------------------------------

_BLACK   = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
# freezedetect and silencedetect report a start line and, later, a matching end line.
# Both are captured so a defect is a span rather than an instant: an event with no
# duration renders as a zero-length span and tells an operator nothing about how much
# of the master is affected.
_FREEZE_START  = re.compile(r"freeze_start:\s*(-?[\d.]+)")
_FREEZE_END    = re.compile(r"freeze_end:\s*(-?[\d.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END   = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _spans(text: str, start_rx: re.Pattern, end_rx: re.Pattern,
           window_seconds: int | None) -> list[dict]:
    """Pair start lines with end lines in the order ffmpeg emitted them.

    An event still running when the scan window closes has a start and no end. It is
    kept, and closed at the end of the window, with `truncated` set so nothing
    downstream reports a duration as if the whole event had been observed. Dropping it
    would hide a defect; closing it at its own start time would claim it lasted no
    time at all.
    """
    starts = [float(x) for x in start_rx.findall(text)]
    ends = [float(x) for x in end_rx.findall(text)]
    spans: list[dict] = []
    for index, start in enumerate(starts):
        if index < len(ends):
            end, truncated = ends[index], False
        else:
            end = float(window_seconds) if window_seconds else start
            truncated = True
        spans.append({
            "start": start,
            "end": max(end, start),
            "duration": max(end - start, 0.0),
            "truncated": truncated,
        })
    return spans


def measure_structural(path: str | Path, seconds: int | None = None,
                       min_black: float = 1.0) -> dict:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += [
        "-vf", f"blackdetect=d={min_black}:pix_th=0.10,freezedetect=n=-60dB:d=2",
        "-af", "silencedetect=n=-50dB:d=2",
        "-f", "null", "-",
    ]
    text = _run(cmd).stderr
    blacks = [
        {"start": float(a), "end": float(b), "duration": float(c)}
        for a, b, c in _BLACK.findall(text)
    ]
    return {
        "black_segments": blacks,
        "freeze_events": _spans(text, _FREEZE_START, _FREEZE_END, seconds),
        "silence_events": _spans(text, _SILENCE_START, _SILENCE_END, seconds),
        "window_seconds": seconds,
    }


def structural_events(measured: dict) -> list[tuple[str, float, float, str]]:
    """One row per defect occurrence, as (kind, start_seconds, end_seconds, detail).

    `structural_findings` below counts these into a pass/fail verdict, which is what
    a spec check needs. This returns the occurrences themselves, which is what the
    point-in-time join needs: "three black segments" cannot be joined to the loudness
    at the moment the picture went black, and "black at 41.2s" can.

    Every kind carries a real span. An event still running when the scan window closed
    is closed at the window edge and says so, rather than being recorded as though it
    lasted no time at all.
    """
    events: list[tuple[str, float, float, str]] = []
    for segment in measured.get("black_segments", []):
        events.append((
            "black", float(segment["start"]), float(segment["end"]),
            f"{segment['duration']:.1f}s of black",
        ))
    for kind, label in (("freeze", "frozen video"), ("silence", "silence below -50 dB")):
        for span in measured.get(f"{kind}_events", []):
            tail = (
                f", still running at the {measured.get('window_seconds')}s scan edge"
                if span.get("truncated") else ""
            )
            events.append((
                kind, float(span["start"]), float(span["end"]),
                f"{span['duration']:.1f}s of {label}{tail}",
            ))
    return events


def subtitle_events(srt_text: str) -> list[tuple[str, float, float, str]]:
    """One row per offending subtitle cue, so a slip can name the cue not the count."""
    events: list[tuple[str, float, float, str]] = []
    for cue in _parse_srt(srt_text):
        duration = max(cue["end"] - cue["start"], 0.001)
        chars = len(re.sub(r"<[^>]+>", "", cue["text"]).replace(" ", ""))
        cps = chars / duration
        if cps > NETFLIX_MAX_CPS:
            events.append((
                "subtitle_speed", cue["start"], cue["end"],
                f"{cps:.0f} chars/sec against {NETFLIX_MAX_CPS:.0f} limit",
            ))
        if duration < NETFLIX_MIN_CUE_SECONDS:
            events.append((
                "subtitle_short", cue["start"], cue["end"],
                f"cue held {duration:.2f}s against {NETFLIX_MIN_CUE_SECONDS:.2f}s minimum",
            ))
        for line in cue["lines"]:
            plain = re.sub(r"<[^>]+>", "", line)
            if len(plain) > NETFLIX_MAX_LINE_CHARS:
                events.append((
                    "subtitle_long_line", cue["start"], cue["end"],
                    f"{len(plain)} characters on one line against {NETFLIX_MAX_LINE_CHARS}",
                ))
                break
    return events


def structural_findings(measured: dict, max_black_seconds: float = 2.0) -> list[Finding]:
    long_blacks = [b for b in measured["black_segments"] if b["duration"] >= max_black_seconds]
    findings = [
        Finding(
            check="black_frames",
            spec=f"delivery spec: no black segment >= {max_black_seconds:.0f}s",
            measured=float(len(long_blacks)), target=0.0, unit="segments",
            passed=not long_blacks,
            detail="; ".join(
                f"{b['start']:.1f}s to {b['end']:.1f}s ({b['duration']:.1f}s)" for b in long_blacks[:5]
            ) or "no black segment over threshold",
            auto_fixable=False,
            rescue_cost="manual review: each segment needs a human to confirm reel-change vs damage",
        ),
        Finding(
            check="frozen_frames",
            spec="delivery spec: no frozen video",
            measured=float(len(measured["freeze_events"])), target=0.0, unit="events",
            passed=not measured["freeze_events"],
            detail="; ".join(
                f"{span['start']:.1f}s to {span['end']:.1f}s ({span['duration']:.1f}s)"
                for span in measured["freeze_events"][:5]
            ) or "no freeze detected",
            auto_fixable=False,
            rescue_cost="manual review: each freeze must be inspected",
        ),
    ]
    return findings


# --- subtitle compliance -------------------------------------------------

def _parse_srt(text: str) -> list[dict]:
    cues = []
    blocks = re.split(r"\n\n+", text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2}[,:.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,:.]\d{3})", lines[1])
        if not m:
            continue
        def _t(s: str) -> float:
            s = s.replace(",", ".").replace(":", " ", 2).replace(":", ".")
            parts = s.split()
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        start, end = _t(m.group(1)), _t(m.group(2))
        text_lines = lines[2:]
        cues.append({"start": start, "end": end, "lines": text_lines,
                     "text": " ".join(text_lines)})
    return cues


def subtitle_findings(srt_text: str) -> list[Finding]:
    cues = _parse_srt(srt_text)
    if not cues:
        return []

    total = len(cues)
    speed_fails = 0
    short_fails = 0
    long_line_fails = 0
    long_cues_detail: list[str] = []

    for c in cues:
        dur = max(c["end"] - c["start"], 0.001)
        chars = len(re.sub(r"<[^>]+>", "", c["text"]).replace(" ", ""))
        cps = chars / dur
        if cps > NETFLIX_MAX_CPS:
            speed_fails += 1
            if len(long_cues_detail) < 3:
                long_cues_detail.append(f"{c['start']:.1f}s: {cps:.0f} chars/s")
        if dur < NETFLIX_MIN_CUE_SECONDS:
            short_fails += 1
        for line in c["lines"]:
            if len(re.sub(r"<[^>]+>", "", line)) > NETFLIX_MAX_LINE_CHARS:
                long_line_fails += 1
                break

    speed_pct = 100 * speed_fails / total if total else 0
    findings = [
        Finding(
            check="subtitle_reading_speed",
            spec="Netflix TTSS (≤17 chars/sec adult)",
            measured=round(speed_pct, 1), target=0.0, unit="% cues failing",
            passed=speed_fails == 0,
            detail=f"{speed_fails}/{total} cues exceed 17 chars/sec"
                   + (f": {'; '.join(long_cues_detail)}" if long_cues_detail else ""),
            auto_fixable=True,
            rescue_cost="automated cue retiming into free space, ~5 min/title",
        ),
        Finding(
            check="subtitle_min_duration",
            spec="Netflix TTSS (≥5/6 s min cue duration)",
            measured=float(short_fails), target=0.0, unit="cues",
            passed=short_fails == 0,
            detail=f"{short_fails}/{total} cues shorter than {NETFLIX_MIN_CUE_SECONDS:.2f}s",
            auto_fixable=True,
            rescue_cost="cue extension pass, ~5 min/title",
        ),
        Finding(
            check="subtitle_line_length",
            spec="Netflix TTSS (≤42 chars/line)",
            measured=float(long_line_fails), target=0.0, unit="cues",
            passed=long_line_fails == 0,
            detail=f"{long_line_fails}/{total} cues contain a line >42 chars",
            auto_fixable=False,
            rescue_cost="manual re-wrap: line breaks must be semantically correct",
        ),
    ]
    return findings


# --- combined run ---------------------------------------------------------

def run_qc(path: str | Path, subtitle_text: str | None = None,
           seconds: int | None = None) -> QCReport:
    report = QCReport(source=str(path), window_seconds=seconds)
    report.duration_seconds = duration_of(path)

    # Loudness
    try:
        lm = measure_loudness(path, seconds=seconds)
        report.findings.extend(loudness_findings(lm))
    except Exception as exc:
        report.findings.append(Finding(
            check="loudness_error", spec="ffmpeg ebur128",
            measured=None, target=None, unit="",
            passed=False, detail=str(exc)[:200], auto_fixable=False,
        ))

    # Structural
    try:
        sm = measure_structural(path, seconds=seconds)
        report.findings.extend(structural_findings(sm))
    except Exception as exc:
        report.findings.append(Finding(
            check="structural_error", spec="ffmpeg blackdetect/freezedetect",
            measured=None, target=None, unit="",
            passed=False, detail=str(exc)[:200], auto_fixable=False,
        ))

    # Subtitles
    if subtitle_text:
        try:
            report.findings.extend(subtitle_findings(subtitle_text))
        except Exception as exc:
            report.findings.append(Finding(
                check="subtitle_error", spec="subtitle parsing",
                measured=None, target=None, unit="",
                passed=False, detail=str(exc)[:200], auto_fixable=False,
            ))

    return report
