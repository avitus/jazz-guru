"""Weimar Jazz Database lick-matching data layer.

Owns the on-disk shape of ``data/wjazzd/wjazzd-index.json`` and the only
sanctioned load/search path. The agent goes through the ``lick_match`` tool
surface (``actions/tools/lick_match.py``) rather than reading the index via
``fs_read``/``python_exec``.

The matching algorithm is a port of the n-gram inverted-index melodic search
from the ``avitus/mankunku`` repo (``src/lib/matching/{encode,search}.ts``):
phrases are reduced to transposition- and tempo-invariant feature vectors
(semitone intervals + quantized inter-onset intervals), and a query is scored
against every alignment that shares an interval n-gram.

Cache invalidates automatically when the index file mtime changes; tests can
force a reload via :func:`clear_index_cache`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from jazz_guru.config import get_settings

# --- tunables (kept in sync with mankunku's search.ts) ----------------------
NGRAM_SIZE = 5
DEFAULT_MIN_SCORE = 0.75
DEFAULT_TOP_K = 3
MAX_SEQUENCE_LENGTH = 512
SIXTEENTHS_PER_BEAT = 4
# score >= this is reported as a literal quote rather than "reminiscent of"
QUOTE_CONFIDENCE_THRESHOLD = 0.9


class SourceEntry(BaseModel):
    """One transcribed solo (or curated quote) in the corpus."""

    id: str
    kind: str
    performer: str
    title: str
    key: str | None = None
    year: int | None = None
    note: str | None = None


class IndexPhrase(BaseModel):
    """The feature vector for one source, parallel to :class:`SourceEntry` by id."""

    sourceId: str
    startBar: int | None = None
    intervals: list[int]
    iois: list[int]


@dataclass
class MatchIndex:
    sources: list[SourceEntry]
    phrases: list[IndexPhrase]
    sources_by_id: dict[str, SourceEntry]
    # interval n-gram (comma-joined) -> [(phrase_index, position_within_phrase), ...]
    ngram_index: dict[str, list[tuple[int, int]]]
    ngram_size: int


@dataclass
class MatchResult:
    source_id: str
    source: SourceEntry
    start_bar: int | None
    score: float
    matched: int
    query_length: int


_cache: tuple[Path, float, MatchIndex] | None = None


def index_path() -> Path:
    """Location of the committed WJazzD matching index."""
    return Path(get_settings().jg_data_dir) / "wjazzd" / "wjazzd-index.json"


def clear_index_cache() -> None:
    global _cache
    _cache = None


def _build_ngram_index(
    phrases: list[IndexPhrase], ngram_size: int
) -> dict[str, list[tuple[int, int]]]:
    ngram_index: dict[str, list[tuple[int, int]]] = {}
    for p_idx, phrase in enumerate(phrases):
        intervals = phrase.intervals
        for pos in range(0, len(intervals) - ngram_size + 1):
            key = ",".join(str(x) for x in intervals[pos : pos + ngram_size])
            ngram_index.setdefault(key, []).append((p_idx, pos))
    return ngram_index


def load_index() -> MatchIndex:
    """Read + build the match index. (path, mtime)-keyed cache; safe to call hot."""
    global _cache
    p = index_path()
    if not p.exists():
        raise FileNotFoundError(
            f"WJazzD index not found at {p}. Expected data/wjazzd/wjazzd-index.json "
            "to be committed (see data/wjazzd/ATTRIBUTION.md)."
        )
    mtime = p.stat().st_mtime
    if _cache is not None and _cache[0] == p and _cache[1] == mtime:
        return _cache[2]

    raw = json.loads(p.read_text(encoding="utf-8"))
    sources = [SourceEntry.model_validate(s) for s in raw.get("sources", [])]
    phrases = [IndexPhrase.model_validate(ph) for ph in raw.get("phrases", [])]
    index = MatchIndex(
        sources=sources,
        phrases=phrases,
        sources_by_id={s.id: s for s in sources},
        ngram_index=_build_ngram_index(phrases, NGRAM_SIZE),
        ngram_size=NGRAM_SIZE,
    )
    _cache = (p, mtime, index)
    return index


# --- encoding ---------------------------------------------------------------


def quantize_ioi(delta_beats: float) -> int:
    """Inter-onset interval in 16th-note ticks, floored at 1 (round-half-up)."""
    return max(1, math.floor(delta_beats * SIXTEENTHS_PER_BEAT + 0.5))


def encode_notes(pitches: list[int], onsets_beats: list[float]) -> tuple[list[int], list[int]]:
    """Encode a monophonic line into (intervals, iois).

    ``pitches`` and ``onsets_beats`` must be parallel, already rest-filtered,
    and ordered by onset. ``onsets_beats`` is in quarter-note beats from the
    phrase start. Returns two parallel arrays of length ``len(pitches) - 1``.
    """
    if len(pitches) != len(onsets_beats):
        raise ValueError("pitches and onsets_beats must have equal length")
    intervals: list[int] = []
    iois: list[int] = []
    for i in range(1, len(pitches)):
        intervals.append(pitches[i] - pitches[i - 1])
        iois.append(quantize_ioi(onsets_beats[i] - onsets_beats[i - 1]))
    return intervals, iois


def encode_midi(path: Path) -> tuple[list[int], list[int]]:
    """Encode a Standard MIDI File's note line into (intervals, iois).

    Treats every ``note_on`` (velocity > 0) as a melody note, ordered by
    absolute tick. Intended for monophonic licks; chords are flattened in
    tick/pitch order.
    """
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(str(path))
    tpb = mid.ticks_per_beat or 480
    onsets: list[tuple[int, int]] = []  # (abs_tick, pitch)
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                onsets.append((abs_tick, msg.note))
    onsets.sort(key=lambda e: (e[0], e[1]))
    pitches = [pitch for _, pitch in onsets]
    onsets_beats = [tick / tpb for tick, _ in onsets]
    return encode_notes(pitches, onsets_beats)


# --- search -----------------------------------------------------------------


@dataclass
class _AlignmentStats:
    matched: int
    interval_hits: int
    rhythm_hits: int


def _align_score(
    q_intervals: list[int],
    q_iois: list[int],
    p_intervals: list[int],
    p_iois: list[int],
    offset: int,
) -> _AlignmentStats:
    """Score one query/phrase alignment over their overlapping span."""
    start_q = max(0, -offset)
    start_p = max(0, offset)
    end_q = min(len(q_intervals), len(p_intervals) - offset)
    matched = end_q - start_q
    if matched <= 0:
        return _AlignmentStats(0, 0, 0)
    interval_hits = 0
    rhythm_hits = 0
    for k in range(matched):
        if q_intervals[start_q + k] == p_intervals[start_p + k]:
            interval_hits += 1
        if abs(q_iois[start_q + k] - p_iois[start_p + k]) <= 1:
            rhythm_hits += 1
    return _AlignmentStats(matched, interval_hits, rhythm_hits)


def search(
    intervals: list[int],
    iois: list[int],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    top_k: int = DEFAULT_TOP_K,
    index: MatchIndex | None = None,
) -> list[MatchResult]:
    """Rank corpus solos by melodic similarity to a query feature vector.

    ``intervals`` and ``iois`` must be equal-length integer arrays (see
    :func:`encode_notes`). Returns at most ``top_k`` results, best score
    first, one per source.
    """
    if len(intervals) != len(iois):
        raise ValueError("intervals and iois must have equal length")
    idx = index if index is not None else load_index()
    n = idx.ngram_size
    q = intervals
    q_iois = iois
    if len(q) < n:
        return []

    seen: set[str] = set()
    alignments: list[tuple[int, int]] = []
    for qi in range(0, len(q) - n + 1):
        key = ",".join(str(x) for x in q[qi : qi + n])
        hits = idx.ngram_index.get(key)
        if not hits:
            continue
        for p_idx, pi in hits:
            offset = pi - qi
            align_key = f"{p_idx}:{offset}"
            if align_key in seen:
                continue
            seen.add(align_key)
            alignments.append((p_idx, offset))

    candidates: list[MatchResult] = []
    for phrase_index, offset in alignments:
        phrase = idx.phrases[phrase_index]
        aln = _align_score(q, q_iois, phrase.intervals, phrase.iois, offset)
        if aln.matched == 0:
            continue
        interval_ratio = aln.interval_hits / aln.matched
        rhythm_ratio = aln.rhythm_hits / aln.matched
        raw = 0.7 * interval_ratio + 0.3 * rhythm_ratio
        length_penalty = math.sqrt(aln.matched / len(q))
        score = raw * length_penalty
        if score < min_score:
            continue
        source = idx.sources_by_id.get(phrase.sourceId)
        if source is None:
            continue
        candidates.append(
            MatchResult(
                source_id=phrase.sourceId,
                source=source,
                start_bar=phrase.startBar,
                score=score,
                matched=aln.matched,
                query_length=len(q),
            )
        )

    # Keep the best alignment per source, then sort by score descending.
    by_source: dict[str, MatchResult] = {}
    for r in candidates:
        cur = by_source.get(r.source_id)
        if cur is None or r.score > cur.score:
            by_source[r.source_id] = r
    ranked = sorted(by_source.values(), key=lambda r: r.score, reverse=True)
    return ranked[:top_k]


# --- presentation -----------------------------------------------------------


def format_label(source: SourceEntry, start_bar: int | None) -> str:
    if source.kind == "wjazzd":
        bar = f", bar {start_bar}" if start_bar else ""
        return f"{source.performer} — {source.title}{bar}"
    return source.title


def format_attribution(source: SourceEntry) -> str:
    if source.kind == "wjazzd":
        year = f", {source.year}" if source.year else ""
        return f"Weimar Jazz Database: {source.performer} — {source.title}{year}"
    return source.note or "Curated quote"
