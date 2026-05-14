# Weimar Jazz Database attribution

`wjazzd-index.json` is a compact index derived from the **Weimar Jazz
Database** (WJazzD), a collection of transcribed jazz solos maintained by
the Jazzomat Research Project at the Hochschule für Musik Franz Liszt
Weimar.

- Project page: <https://jazzomat.hfm-weimar.de/>
- Database downloads: <https://jazzomat.hfm-weimar.de/dbformat/dbdownload.html>
- Citation: Pfleiderer, M.; Frieler, K.; Abeßer, J.; Zaddach, W.-G.; Burkhart, B. (Eds.), *Inside the Jazzomat — New Perspectives for Jazz Research*. Schott Campus, 2017.

## Provenance

Two artifacts derive from WJazzD:

- `wjazzd-index.json` — the compact matching index, copied from the
  `avitus/mankunku` repo (`src/lib/matching/data/wjazzd-index.json`). Per
  its `_readme` field: 456 solos, 456 phrases, built 2026-04-23.
- `wjazzd.db` — the raw WJazzD SQLite file (v2.1 / DB version 2.2),
  downloaded from the official Jazzomat download page. **Gitignored** (it
  matches `*.db` in `.gitignore`) and intentionally not committed; the
  index above is the committed, redistributable form.

## License

The Weimar Jazz Database is released under the **Open Data Commons Open
Database License (ODbL) v1.0**, per the official Jazzomat download page.
Full license text: <https://opendatacommons.org/licenses/odbl/1-0/>.

> Note: an earlier copy of this file (inherited from `avitus/mankunku`)
> labeled WJazzD as CC-BY-NC-SA 4.0. That was incorrect — the authoritative
> download page states ODbL. ODbL is less restrictive: it permits
> commercial use. The `avitus/mankunku` copy of this doc still carries the
> wrong license and should be corrected there too.

`wjazzd-index.json` is an adapted database under ODbL and inherits the
same license. Implications:

- **Attribution.** Any feature that surfaces a match must carry the source
  performer + title so WJazzD credit follows the data wherever it appears.
  See the citation above for formal attribution.
- **Share-alike.** Any adapted/modified database derived from WJazzD must
  be released under ODbL.
- **Keep open.** If you publicly redistribute the database (or an adapted
  version), you must also make the underlying data available under ODbL
  and may not use technical measures that restrict access to it.
- **Commercial use is permitted** under ODbL, provided the above hold.

## Rebuilding the index

The raw `wjazzd.db` is present locally but gitignored. To regenerate the
index, see the `avitus/mankunku` repo's `scripts/build-wjazzd-index.mjs`:

1. Download `wjazzd.db` from the Jazzomat download page above (or use the
   gitignored copy in this directory).
2. Run the mankunku build script against it.
3. Copy the resulting `wjazzd-index.json` back here.

## File format

`wjazzd-index.json` is a JSON object with three keys (`_readme`,
`sources`, `phrases`):

```
_readme : str          — build provenance + license note
sources : SourceEntry[] — one per transcribed solo
phrases : IndexPhrase[] — one per solo, parallel to sources by sourceId

SourceEntry:
  id        : str        — e.g. "wjazzd:1"
  kind      : "wjazzd" | "quote"
  performer : str        — e.g. "Charlie Parker"
  title     : str        — e.g. "Ko Ko"
  key       : str?       — original key, display only (e.g. "Bb-maj")
  year      : int?       — recording year
  note      : str?       — curated-quote explanation (quote sources only)

IndexPhrase:
  sourceId  : str        — references SourceEntry.id
  startBar  : int?       — starting bar within the source (0/1-based when known)
  intervals : int[]      — semitone intervals between consecutive pitched notes (length N-1)
  iois      : int[]      — inter-onset intervals in 16th-note ticks (length N-1)
```

The interval/IOI representation is transposition- and tempo-invariant,
which is what makes it usable for melodic matching.
