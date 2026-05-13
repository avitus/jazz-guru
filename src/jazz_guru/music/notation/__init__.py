"""Symbolic-notation helpers (MIDI, MusicXML, lead sheets).

Convenience shims that sit next to the existing
:mod:`jazz_guru.actions.tools.midi` and ``music_xml`` tools — they reuse
the same ``music21`` / ``mido`` plumbing so the music backend layer has
a single, typed entry point for symbolic work without duplicating logic.
"""
from __future__ import annotations

from jazz_guru.music.notation.leadsheet import LeadSheet, load_leadsheet
from jazz_guru.music.notation.midi import midi_note_count, midi_summary
from jazz_guru.music.notation.musicxml import musicxml_summary

__all__ = [
    "LeadSheet",
    "load_leadsheet",
    "midi_note_count",
    "midi_summary",
    "musicxml_summary",
]
