**Architecture, UX, and roadmap review — 19 September 2026**

Reviewed commit `d77dd2d`, including the roadmap, pipeline, HTTP handlers,
generated frontend, tests, and Nix service configuration. Assumption: this
remains a personal library on a home server, as described in the roadmap.

The processing pipeline and static reader are a good foundation. Preserve the
current reading layout. The main investment should be in identity, job recovery,
safe publication, and library management. Most of the problems below come from
using filenames and whole-library rebuilds to coordinate operations that now
need their own persistent state.

Validation: `nix develop -c make test` passed all 137 Python tests and all three
JavaScript runtime suites. Additional temporary reproductions exercised cache
invalidation, renaming and pruning, error propagation, concurrent uploads,
filename addressing, upload replacement, and export paths. Expensive media and
model calls were mocked in the pipeline reproductions. UX observations are from
source and runtime harnesses; this was not a visual browser audit or a new
end-to-end transcription run. Application code and ROADMAP.md were not changed.

**Concrete findings, in priority order**

1. **P1 — A management request can delete the wrong imported file.**
   `ingest.py:46–59,166–191` sanitizes the filename supplied to DELETE and POST,
   rather than resolving the exact existing filename. Files copied through SSH
   have not passed through that sanitizer. With both `What's next.mp4` and
   `What_s next.mp4` present, deleting the former through its listed API URL
   deletes the latter and returns 200. Reproduced using the actual delete
   handler. Course names have the same addressing problem. Address existing
   records by stable IDs; until then, decode and validate existing names without
   changing them. Keep name normalization separate from lookup, and enforce
   containment within the library, including symlink handling.

2. **P1 — Renaming a lesson can break all of its media references.**
   `ingest.py:256–274` moves its stage cache. `pipeline.py:326–339` accepts cached
   assets because their old paths still exist, then `render.py:2165–2171` prunes
   those old paths after publishing the renamed lesson. Reproduced: the new
   lesson referenced `frames/old/...`; every referenced file existed before
   publication and was gone afterward. This is more serious than the roadmap's
   stated worst case of a re-transcription. Use asset locations independent of
   display names. A short-term fix must migrate/rewrite asset references or
   invalidate the asset stage during rename, with a regression test that resolves
   every media URL after pruning.

3. **P1 — Failure handling has three incompatible outcomes.**
   `whisper.py:188–199` converts a subprocess failure into `SystemExit`, which
   bypasses the per-video handler and is explicitly re-raised by the watcher
   (`cli.py:329–330`). A failed Whisper invocation can restart the service,
   including its upload API. An `LLMError` escapes the per-video loop
   (`pipeline.py:528–542`), so a permanently invalid lesson prevents later lessons
   from being attempted. Conversely, caught `CommandError`s mark lessons failed
   but let the watcher set `done = state` (`cli.py:326–328`), preventing another
   attempt until the library changes. All three paths were reproduced. Use typed
   per-job outcomes, durable attempts, bounded retries for transient failures,
   and quarantine for persistent failures. Keep `SystemExit` at the CLI boundary.

4. **P1 — Concurrent uploads to one destination can corrupt an accepted upload.**
   `ingest.py:313–342` checks existence before receiving the body, then uses the
   same `.incoming-<name>` path for every request. Two requests can both pass the
   check and open the same temporary file. A controlled interleaving reproduced
   a 201 for upload A followed by a 500 for upload B, with B's bytes nevertheless
   replacing A's published contents. Use unique upload IDs and temporary paths,
   a destination reservation or lock, and a conflict check at commit. Apply
   comparable coordination to rename, overwrite, delete, and active processing.

5. **P1 — Cache keys omit upstream dependencies.**
   `pipeline.py:163–171,225–239` keys extracted steps by source stat and LLM
   settings, without identifying the transcript they consume. Re-transcribing
   with a different Whisper model/language, editing a transcript cache, or forcing
   transcription can leave the old steps in use. Reproduced two transcription
   calls producing different text but only one step extraction; the published
   transcript changed while the guide remained based on the old one. The asset
   key also omits step motion flags and transcript data used by clip windows
   (`pipeline.py:309–325`). Include hashes of actual upstream inputs and stage
   implementation versions. A forced stage should invalidate dependants when
   its output changes, while unrelated branches remain reusable.

6. **P1 — A failed rebuild can damage the currently published lesson.**
   Frames and clips are overwritten directly in served directories
   (`pipeline.py:306–307,365–376,399–417`), and HTML, JSON, scripts, and CSS are
   written directly to their public names (`render.py:2131–2137,2216–2239`).
   A reader can receive incomplete files or old instructions paired with new
   media. A later failure can leave that mixed version in place. This follows
   directly from the write paths; a process-kill reproduction was not needed.
   Generate each lesson revision in a staging directory, validate its references,
   then publish it atomically with immutable media paths. Publish completed
   lessons independently so the first lesson is readable before a long batch
   finishes. Retain the previous revision until publication succeeds.

7. **P2 — Reading progress collides across courses and changes meaning on rebuild.**
   `render.py:542,1055–1078,2017` uses only the lesson slug for localStorage and
   sequential `step-1` identifiers for completion. Two courses containing
   `01-intro.mp4` share the key `v2w:01-intro`; confirmed from generated pages.
   Rename loses state, and changing generated step boundaries can silently attach
   completion to different instructions. Introduce stable lesson identity now,
   before adding synchronization. Associate step state with a content revision
   and define an explicit policy for progress after regeneration.

8. **P2 — Deleting the final video never clears the published library.**
   `_watch_decision()` only builds a nonempty state (`cli.py:245–249`). After the
   final deletion, the observed transitions are `settle`, then `idle`, so old
   course pages and indexes remain. `build()` also rejects an empty library and
   only publishes/prunes when there are successful lessons
   (`pipeline.py:497–499,550–556`). Treat an empty library as a valid reconciliation
   result and publish the empty state without invoking the media pipeline.

9. **P2 — Preserving a failed lesson's files does not preserve its discoverability.**
   The pruning safeguard keeps attempted lessons on disk, but indexes are
   regenerated from successful lessons only (`pipeline.py:539–555`,
   `render.py:2213–2239`). Reproduced an existing lesson page surviving while its
   link vanished from the course index. A whole failed course can similarly
   disappear from the home page. Build navigation from the catalog and last
   successful revisions, with processing/failure state displayed separately.

10. **P2 — “Replace it” bypasses the upload queue.**
    `render.py:1682–1715` starts a replacement directly with `send()` after
    advancing the queue. Reproduced two active requests despite the sequential
    upload design; when the replacement finishes first, `busy` becomes false
    while the other upload is still running, disabling the navigation warning.
    The queue also says “Done” after failures, and the asynchronous library
    refresh immediately clears that message. Put retry/replace through the same
    queue state machine and derive completion from actual job outcomes.

11. **P2 — A valid CLI argument can make window planning loop indefinitely.**
    `--chunk-minutes` accepts arbitrary floats (`cli.py:38`), while
    `steps.py:24–35` subtracts a fixed 45-second overlap. At `--chunk-minutes 0.5`,
    a 120-second lesson produces cursors `0, -15, -30, -45, ...`; reproduced with
    a trace that stops after five iterations. Validate positive, finite settings
    and require the window span to exceed the overlap. Validate widths, rates,
    and intervals at the same boundary.

12. **P2 — Markdown media links resolve against the wrong directory.**
    `render.py:2122–2126` emits `frames/...` and `clips/...`, but the export is
    written to `<course>/md/<lesson>.md` (`render.py:2219–2222`). Those links resolve
    inside `md/`, where the media does not exist. Confirmed from generated output.
    Generate paths relative to the export's location; provide a visible download
    link and, if portability is intended, a bundle containing its media.

**Architecture recommendation**

Keep Python, ffmpeg, whisper.cpp, model adapters, reusable stages, nginx, and the
static reader. The strongest parts already include streaming uploads, atomic
stage JSON writes, explicit rendering escapes, on-demand media loading, and
reproducible service packaging.

Introduce a small application layer around those components:

| Component | Responsibility |
| --- | --- |
| Library/catalog API | Stable course and lesson IDs; titles, chapters, ordering, source paths, trash, upload sessions |
| SQLite database | Catalog metadata, durable jobs and attempts, current revision references, later reading state |
| One worker initially | Claim jobs, execute stages, record errors/heartbeats, retry or quarantine, publish revisions |
| Filesystem scanner | Discover SSH imports and reconcile externally changed or missing sources into the catalog |
| Original files and stage artifacts | Remain ordinary files; JSON outputs stay inspectable and disposable |
| Static reader | Render the same visual design from published lesson revisions |

SQLite is a reasonable fit for this single-server workload. Its documentation
specifically supports application-server use and explains that write transactions
serialize: keep them short and never hold a transaction while ffmpeg or a model
is running. Keep the database on local storage, even if media lives on a network
share. See [SQLite's deployment guidance](https://www.sqlite.org/whentouse.html).

The roadmap's decision to retain stage artifacts as files is sound. The statement
that SQLite is appropriate *only* for reading state is too restrictive now that
the application needs job attempts, stable identity, ordering, and concurrent
management operations. These records describe user intent and execution history;
they cannot all be reconstructed from `stat()`.

Assign ownership clearly: original files supply media; the database owns identity,
organization, and job state; generated files are derived output. The scanner is
an importer/reconciler with explicit rules for external moves and deletion. A
course manifest can be a portable import/export format, but should not be a
second independently editable authority for the same metadata. This adds a
reconciliation responsibility, but the current code already has consistency
problems between the filesystem, stage caches, published site, and browser state.

Separate IDs, titles, filenames, ordering, and source revisions. Renaming a title
or moving a lesson between chapters should update metadata and navigation without
running transcription or regenerating media. Compute a content digest during
upload, or once during an external import, and use stat information as a cheap
change detector. A size/mtime pair is neither a content digest nor a stable
identity across re-uploads.

Run the API and worker in separate processes so a processing failure does not
terminate an upload. Start with a single worker and a durable queue. Give active
jobs a lease/heartbeat, recover abandoned jobs after restart, and distinguish
retryable errors from invalid input. Publish only if the source revision is still
current; cancellation or deletion must prevent a stale worker result from making
the lesson reappear. Keep source versions immutable while jobs use them, or defer
destructive mutations under a per-lesson lock.

For publishing, a temporary file plus replacement can atomically update one file,
but a lesson contains multiple files. Use immutable revision directories and
switch a small public page/manifest reference only after all artifacts exist.
Python documents the atomic replacement primitive and its same-filesystem
constraint in [os.replace](https://docs.python.org/3/library/os.html#os.replace).
Retain old revisions long enough for open pages and clean them up separately.

Move CSS and JavaScript out of the 2,254-line `render.py` into packaged assets,
and separate page templates from pipeline data structures. Add typed records and
validate model output before accepting it. These changes improve maintenance
without requiring a different visual design. A small web framework is an option
as the API grows; stable state and transaction boundaries matter more than which
framework is chosen.

**UX: build around the whole import-to-reading journey**

The present management page exposes implementation details: course folders,
filenames, and renaming to reorder. It lacks a durable view of a lesson's journey
from selection to a readable page. Preserve the existing typography, cards, and
reader, and develop three management surfaces:

| Surface | First useful version |
| --- | --- |
| Library | Search courses/lessons; sort; status and progress filters; continue reading; add-course action; storage usage |
| Course management | Editable title; chapters; explicit lesson order with keyboard-accessible move controls; add/move lessons; bulk actions; per-lesson status and open/retry actions |
| Import/activity | File or folder selection; destination and chapter preview; validation; duplicate choices; aggregate and per-file transfer progress; persistent processing history |

For uploads, separate “uploaded” from “ready to read.” Use explicit states:
selected, uploading, received, queued, processing, ready, failed, canceled.
Show transfer bytes/speed separately from the processing stage. Queue position
and stage descriptions are more honest than assigning equal percentages to
stages with radically different durations. The current elapsed display freezes
between status writes, and an old `status.json` can imply the worker is still
running; show a last-update indicator and detect a stale heartbeat.

Before transfer, preview folder-to-course/chapter mapping and the final filenames,
identify unsupported files, and resolve duplicates with skip/replace/keep-both
choices. Offer cancel for active uploads, remove for queued items, retry failed
items, and batch outcomes such as “8 uploaded, 2 need attention.” Keep processing
status and the eventual lesson link on this surface, instead of sending users to
the home page to find out what happened.

Add resumable transfer after queue correctness, cancellation, and retry. Whether
it deserves the first release depends on real file sizes and connection failures;
the roadmap should not assume every lost upload costs only a minute. Processing
should continue independently after a completed upload even when its tab closes.
A transfer that is still active requires the tab or a separately designed upload
client; make that distinction clear.

For library management, prioritize chapter/order metadata, course rename, moving
lessons, multiselect, and trash with restore. Show whether deletion removes the
source, published page, or cached artifacts. Use a clear retention policy for
trash and an explicit permanent-delete action. Cache cleanup should report space
and regeneration cost separately from source deletion. Nested lessons must be
visible wherever the reader can display them; `course_videos()` currently lists
only direct children while discovery builds recursively.

Keep reader changes focused: previous/next lesson, chapter breadcrumbs, continue
from the last position, and a visible export action. Fix keyboard semantics:
the document-level Enter handler currently intercepts focused buttons and links
(`render.py:1198–1247`), marking the current step complete instead of letting the
control activate. Modal overlays need focus handling and an accessible close
control; clip scrubbing needs a keyboard-accessible range control. These are
behavioral improvements compatible with the current appearance.

**Other observations**

- The accepted input extensions greatly exceed what the raw source player can
  reliably play. The pipeline probes codecs but links the original container
  directly into `<video>`. Add a playback-compatibility decision and a browser
  derivative when needed; conversion suitability and browser playback support
  are separate checks. Container and codec both matter, as described in
  [MDN's media-format guide](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Containers).
- Some media failures are treated as successful, cacheable partial results.
  Missing screenshots or clips are omitted and the asset stage is saved; an
  empty asset list passes its existence check on the next run. Record degraded
  output explicitly and allow retrying the failed assets.
- Content quality needs its own evaluation loop. The model receives speech and
  scene timestamps, not visual evidence, so it cannot reliably recover unspoken
  values or actions. Keep transcript timestamps as provenance, support small
  persistent corrections, and use an annotated sample set to measure missed
  actions, incorrect values, and clip/checkpoint usefulness before tuning models.
  Quality measurements should precede an optional visual-analysis stage.
- “Final-state screenshot” is not guaranteed. With no scene candidates and a
  single screenshot, a 60-second step selects 30 seconds; reproduced. Candidate
  times are also sorted before deduplication, so an earlier similar image can
  displace the intended final checkpoint. Protect the chosen checkpoint during
  selection and deduplication.
- Long LLM runs cache only the completed lesson, so failure in the last window
  repeats earlier requests. Cache validated window results with their input and
  prompt hashes. Enforce an object schema as well as parseable JSON, and reject
  nonfinite timestamps and unresolved duplicate/overlapping steps.
- Reserve generated route names such as `index` and `assets`, or use namespaces
  based on IDs. A lesson named `index.mp4` currently competes with the course's
  `index.html`.
- The current LAN-only, unauthenticated deployment is an explicit scope choice.
  Record that assumption instead of treating it as permanent for a shared or
  hosted edition. The concrete file-addressing and concurrency bugs above matter
  even with one trusted user. No authentication expansion is needed to fix them.
- Documentation overstates several guarantees: reading position is not currently
  stored in localStorage; caches do not follow arbitrary external renames; a
  cached LLM title does not follow a filename rename; publishing is not atomic;
  and screenshot documentation describes a 64-bit hash/default distance 8 while
  code uses 256 bits/default 16. After introducing metadata or user corrections,
  backups must cover that state too, not just source videos. The raw
  `whisper.json` listed as orphaned in the roadmap is still deliberately produced
  by `whisper.py` and retained by the pipeline.

**Recommended roadmap order and acceptance criteria**

| Milestone | Scope | Completion criterion |
| --- | --- | --- |
| 1. Trustworthy lifecycle | Exact addressing, concurrent-upload safety, rename/media fix, cache dependencies, error isolation, empty-library reconciliation, safe publication | Rename preserves every media URL; deleting one item never affects another; killing/restarting processing retains a usable published revision; later lessons proceed after one fails |
| 2. Identity and durable jobs | Stable IDs, catalog metadata, job attempts, recovery/quarantine, per-lesson publication, revision-aware progress | Rename/reorder changes no lesson identity; restart recovers queued work; canceled/deleted revisions cannot publish; successful lessons appear as they finish |
| 3. Upload and library UX | Folder import preview, duplicate resolution, cancellation/retry, searchable library, bulk actions, trash, status and open links | A user can import a course, resolve failures, organize it, and undo a deletion entirely through the interface |
| 4. Chapters and reading continuity | Explicit chapter/order metadata, previous/next, saved reading position, then optional cross-device sync | Organization changes preserve progress and caches; returning resumes the intended lesson and position |
| 5. Quality and tuning | Correction overlays, quality fixtures, per-course settings, selective regeneration, optional visual analysis | Corrections survive rebuilds; a setting change reruns only affected work; representative content quality is measured |

Move quarantine and recovery out of “Smaller things” and into milestone 1/2.
Watcher persistence becomes part of durable job recovery. Group cleanup under a
storage/retention policy. Keep the Blender add-on as an optional integration
after the core workflow is dependable.

Retain the fast unit tests, but add a small set of integration tests around
upload → process → publish → rename → delete, interrupted jobs, cache dependency
changes, and two clients targeting the same source. Add a few real-browser tests
for file selection, replacement during a queue, navigation warnings, focused
controls, and seeking. The existing DOM stubs are useful for logic but cannot
validate actual browser focus, media loading, HTTP behavior, or layout.
