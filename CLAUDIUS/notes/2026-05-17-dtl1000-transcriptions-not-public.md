# DTL1000: extracted melodies are not publicly downloadable

Date: 2026-05-17. Investigation triggered by the user asking whether `ppquadrat/DigThatLick` (or any sibling resource) bundles the actual note-level musical content for the DTL1000 set, parallel to what we have for WJazzD.

## Conclusion (don't redo this search)

The DTL1000 note-level transcriptions exist — Basaran et al. CRNN extracted ~1,736 monophonic solos (~300K tone events) — but they have **never been released as a downloadable file**. They live only inside the project's backend, exposed via the web Pattern Similarity Search UI at `dig-that-lick.hfm-weimar.de/similarity_search/`.

The four files in `data/DTL1000/` (segmentations CSV, dtl_1000.json, dtl1000.ttl, metadataErrors.txt) are the complete public DTL1000 release. The 1,685-row `dtl_metadata_v0.9.csv` at the Pattern Search site is the same data flattened to one row per solo, keyed by `solo_id` matching the `dtl_id_seg` format in `dtl_1000.json`.

## Where I looked, so the next agent doesn't repeat

- `github.com/ppquadrat/DigThatLick` → scripts only (Python that builds the TTL).
- OSF `buxvr/rqk7z/{bwg42, 8q59b, 39q2d, nrgb2, dk6w2}` → all metadata/RDF/schema layers. Confirmed via `api.osf.io/v2/nodes/<id>/files/osfstorage/`.
- `jazzomat.hfm-weimar.de/download/download.html` → WJazzD, EsAC, MeloSpy tooling. No DTL1000 listing.
- `dig-that-lick.eecs.qmul.ac.uk` deliverables page → web apps + papers, no data dumps.
- `reshare.ukdataservice.ac.uk/854781/` → no files; redirects back to OSF.
- Pattern Similarity Search docs → only CSV metadata exports, no REST/JSON for note data.

## Why this matters for the architecture

This is the first datastore we've considered where the **typed surface pattern** runs out of road. With WJazzD we can ship `lick_match` because the SQLite has the notes. With DTL1000, the only honest typed surface from public data is a *metadata* lookup ("who played what tune when, with which other musicians, on what instrument, in what year") — useful for biographical/discographical context, but does not give the agent melodic content.

This is also a reminder that the legal/ownership story for jazz datasets is uneven: WJazzD released full transcriptions under ODbL; DTL1000 released metadata under the same shared OSF umbrella but kept the melodies in-house, presumably because the upstream audio rights (Illinois Jazz Institute / Mosaic compilations) made redistribution awkward even for derived note data. Worth defending this distinction in the agent's tool descriptions — `dtl_lookup` should not pretend it can answer melodic questions.

## Concrete next move if we proceed

Building a `dtl_lookup` typed surface against `dtl_1000.json` is straightforward (mirror the `lick_match_info` shape, sandbox under `resolve_in_safe` since `data/DTL1000/` is already a safe root). The metadata vocabulary worth exposing: performer + sidemen + instrument + tune + album + session_date + decade + segment timestamps. A separate `dtl_solo_lookup` keyed by `solo_id` would let the agent join with the segmentation CSV.

If we get academic release of the actual melodies later (worth asking Frieler/Pfleiderer at Weimar, or Dixon at QMUL — they own the backend), porting `lick_match` to DTL1000 should be the next step.

## Tension to track

The web Pattern Similarity Search is a public, scrape-able interface. Building a fragile HTML client around it would give the agent melodic-search-ish capability for DTL1000, but it would (a) violate the project's intent if they wanted the data downloadable they'd have released it, (b) couple us to a UI that can change at any time, (c) bypass the deliberate licensing decision. Worth being honest that this option exists, and worth saying no to it absent explicit blessing from the project.
