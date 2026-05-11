from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace


class MusicXmlInfoInput(BaseModel):
    path: str


class MusicXmlFromTinyInput(BaseModel):
    out_path: str = Field(..., description="Output .mxl or .xml path in workspace.")
    tinynotation: str = Field(..., description="music21 tinyNotation; e.g. '4/4 c4 d e f g1'")
    title: str | None = None


class MusicXmlTransposeInput(BaseModel):
    in_path: str
    out_path: str
    interval: str = Field(..., description="music21 interval string, e.g. 'M3', 'P5', '-m2'.")


def _import_music21() -> Any:
    import music21  # type: ignore[import-not-found]

    return music21


@registry.register(
    "music_xml_info",
    description="Inspect a MusicXML/.mxl/.xml file: time signature, key, parts, measure count, tempo.",
    input_model=MusicXmlInfoInput,
    tags=("music",),
)
async def music_xml_info(path: str) -> dict[str, Any]:
    m21 = _import_music21()
    p = resolve_in_workspace(path, current().session_id)
    score = m21.converter.parse(str(p))
    info: dict[str, Any] = {
        "path": str(p),
        "parts": [pt.partName or pt.id for pt in score.parts],
        "measures": len(list(score.parts[0].getElementsByClass("Measure"))) if len(score.parts) else 0,
    }
    ts = score.recurse().getElementsByClass("TimeSignature").stream()
    if len(ts):
        info["time_signature"] = ts[0].ratioString
    ks = score.recurse().getElementsByClass("KeySignature").stream()
    if len(ks):
        info["key_signature_sharps"] = ks[0].sharps
    tempo = score.recurse().getElementsByClass("MetronomeMark").stream()
    if len(tempo):
        info["tempo_bpm"] = float(tempo[0].number) if tempo[0].number else None
    return info


@registry.register(
    "music_xml_from_tinynotation",
    description="Compose a single-line piece from music21 tinyNotation and write a .mxl/.xml.",
    input_model=MusicXmlFromTinyInput,
    tags=("music",),
)
async def music_xml_from_tinynotation(out_path: str, tinynotation: str, title: str | None = None) -> dict[str, Any]:
    m21 = _import_music21()
    p = resolve_in_workspace(out_path, current().session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    s = m21.converter.parse("tinyNotation: " + tinynotation)
    if title:
        s.metadata = m21.metadata.Metadata()
        s.metadata.title = title
    fmt: Literal["musicxml", "mxl"] = "mxl" if p.suffix.lower() == ".mxl" else "musicxml"
    s.write(fmt, fp=str(p))
    return {"path": str(p), "format": fmt, "elements": len(list(s.recurse().notes))}


@registry.register(
    "music_xml_transpose",
    description="Transpose a MusicXML score by an interval and write to a new file.",
    input_model=MusicXmlTransposeInput,
    tags=("music",),
)
async def music_xml_transpose(in_path: str, out_path: str, interval: str) -> dict[str, Any]:
    m21 = _import_music21()
    pin = resolve_in_workspace(in_path, current().session_id)
    pout = resolve_in_workspace(out_path, current().session_id)
    pout.parent.mkdir(parents=True, exist_ok=True)
    score = m21.converter.parse(str(pin))
    transposed = score.transpose(interval)
    fmt = "mxl" if Path(pout).suffix.lower() == ".mxl" else "musicxml"
    transposed.write(fmt, fp=str(pout))
    return {"in": str(pin), "out": str(pout), "interval": interval, "format": fmt}
