**Durable application architecture**

The service runs Python with two local SQLite databases and DBOS 3.0.0. The
reader remains a static site with its existing layout. One DBOS coordinator runs
on the server; an exclusive process lock enforces that rule. Optional native
helpers process assigned media operations over HTTP and can join or leave while
the server retains local fallback. They do not run DBOS or open its databases.
The databases must be on local storage. DBOS supports SQLite, although its
[documentation](https://docs.dbos.dev/python/tutorials/database-connection)
recommends PostgreSQL for distributed production deployments.

```mermaid
flowchart LR
    UI[Browser management] --> API[Library and worker API process]
    Helper[Optional native helpers] <-->|Inputs, progress, results| API
    API --> L[Library files]
    SSH[SSH import] --> L
    L --> Scanner[Filesystem reconciliation]
    Scanner --> Catalog[(catalog.sqlite)]
    Catalog --> Worker[DBOS coordinator process]
    Worker <--> DBOS[(workflows.sqlite)]
    Worker --> Revisions[Immutable media revisions]
    Revisions --> Reader[Static reader via nginx]
    Worker --> Catalog
```

The catalog owns lesson identity, source identity, build intent, and the last
published lesson. DBOS owns scheduling, step checkpoints, recovery attempts, and
workflow outcomes. Catalog build status is a projection for the application UI,
not a second scheduler. The catalog records a random build ID before enqueueing;
the same ID is the DBOS workflow ID. Reconciliation can safely retry enqueueing
after a crash between the two database commits.

Each lesson revision has one workflow. It checkpoints source preparation, probe,
transcription, scenes, instructions, assets, assembly, and publication. The
ordinary `build` command uses those same stage callbacks without DBOS. Only the
`watch` service supplies the durable runner.

A valid video need not contain instructional actions. Missing audio, a successful
empty transcript, or a successful empty normalized step list produces a video-only
lesson. The existing stages remain in the same order: unnecessary audio/analysis/
LLM work returns empty results, and the frames stage extracts a poster. Publication
still validates the poster and links to the immutable source snapshot. Exceptions
from tools or API calls are not swallowed by this fallback. Cached empty steps
from previously failed jobs remain usable on Retry, with no pipeline-version bump.

Workflow inputs contain a snapshot of processing settings and the source digest.
File operations, model calls, and catalog changes happen inside steps. Network
failures and retryable API responses get up to three step attempts with backoff.
Invalid input and local processing errors fail that lesson and remain available
for explicit retry. DBOS limits crash recovery to three attempts. The next
lesson can proceed independently.

Explicit funding/spend-limit failures are intercepted inside the existing LLM
stage. `ProviderPauses` stores versioned pause identities under `llm-pause:<backend>`
and waiting markers under `llm-wait:<build ID>` in the existing catalog settings
table, so no catalog or DBOS schema changes are needed. Catalog build states stay
`queued`/`running`; the status API adds `blocked` and `provider_pauses` as UI
projections. The request gate rechecks the pause while holding the existing LLM
slot, preventing concurrent callers from repeating the rejected request. Waiting
releases that lock, polls only local SQLite, and observes cancellation/shutdown.
It retains the existing bounded workflow slot instead of starting unbounded work.
Other admitted workflows can continue until they need the paused backend.

`POST /api/providers/<backend>/resume` requires the current `pause_id`. Compare and
delete runs in a SQLite transaction: a stale browser cannot clear a newer pause.
Restoring credits is external; resume simply allows requests again. Already-failed
workflows keep the explicit Retry behavior. Known waiting workflows that exceed
the normal recovery allowance after repeated deployments are re-enqueued with
`DBOS.resume_workflow(..., queue_name="lessons")`; their IDs and checkpoints remain
intact. Other failures still use the normal recovery limit.

LLM section checkpoints are atomic files in `llm-sections/<request hash>.json`,
including a checksum of the accepted response. Their key covers the exact system
and user prompts, backend/model/fallback policy, source fingerprint and instruction
settings. Invalid/partial caches are ignored. Forced builds use their own namespace;
standalone forced runs use a fresh namespace. These files live inside the existing
`steps` stage; no DBOS calls are inserted or reordered. Completed `steps.json`
caches, serialized build specs and pipeline/prompt versions stay compatible.
Saving sections narrows the retry window but cannot recover an API response that
never reached disk, nor retroactively save sections produced by older code.

**Optional processing helpers**

Schema v4 adds worker identities, single-use pairing codes, native task records
and fenced execution attempts. DBOS still owns lesson workflows; a task broker
inside each stage assigns supported native operations and records accepted results.
The server owns both SQLite files and all publication. Helpers receive immutable
inputs, report progress and upload checked results using scoped HTTP endpoints.

The coordinator admits one workflow without helpers and up to one plus the
available helper count, capped at four. Local expensive operations use one CPU
slot. Source preparation/probing/audio extraction have a separate single slot so
new helpers can receive work during a long local transcription. LLM calls are
limited separately to one. Each helper processes one operation at a time.

Within the existing `frames` DBOS stage, a bounded window admits up to eight
independent screenshot-gallery/clip jobs per lesson while media helpers are
available. Completed jobs refill the window immediately; a slow early job cannot
hold back later ready work. Local-only execution admits one job, and joining
helpers can expand the window. Each gallery preserves its own screenshot
deduplication sequence, and motion analysis remains a prerequisite of its clip.
Results merge in lesson order before asset cleanup and publication.

Batch threads receive native execution and cancellation context explicitly,
without inheriting DBOS's workflow context. The parent aggregates progress and
waits for all threads to stop before leaving the stage. Accepted task results
are reused on replay; failed batches fence their outstanding assignments while
coordinator shutdown preserves live remote work. Queue status exposes multiple
executions for one lesson and their step descriptions. Stage names and artifact
cache identities remain unchanged.

Assignments expire after 30 seconds without renewal. A failed/disconnected
attempt falls back locally; completed earlier operations and DBOS stages are
reused. Late results cannot replace the current attempt. Accepted outputs are
stored before checkpoint completion, so recovery can reuse an acknowledged
remote result. Server storage rejection is a capacity error rather than a reason
to repeat expensive processing.

The Workers page lists connected/offline computers, live native tasks, management
controls, and persistent contribution history. The queue links to the assigned
machine. Server heartbeat runs independently of filesystem reconciliation;
processing progress is distinct from liveness.

The connection page downloads a reproducible `.pyz` made from an allowlist of the
installed helper modules. It contains no application state or third-party Python
dependencies. Native FFmpeg/Whisper remain separate; a read-only `--check` and
platform instructions guide their setup. Shared protocol constants live outside
the server's database implementation so the bundle does not include that code.
Pairing codes are generated separately after prerequisite setup.

Schema v5 adds worker archiving. Archiving revokes access and releases active
assignments while keeping history; restoring only unhides the record. Permanent
deletion removes worker/contribution records only when unfinished current builds
do not need their assignments or accepted results. The library and published
revisions remain independent. Fresh pairing codes reuse a saved installation's
identity after verifying its original credential, so connection retries preserve
history instead of creating duplicate workers.

For protocol, setup, retention,
platform support and test coverage, see [DISTRIBUTED_WORKERS.md](DISTRIBUTED_WORKERS.md).

**Identity and artifacts**

Source size/mtime avoid hashing unchanged files on every scan. A changed or new
source is hashed with SHA-256. Its lesson UUID is independent of its filename,
title, and course. API renames preserve that UUID explicitly; external moves
preserve it when there is exactly one unambiguous matching missing source.
Identical copies imported together remain distinct lessons. External course
folder renames currently create a new course group while preserving matching
lesson IDs.

Before processing, the worker copies the source to an immutable snapshot and
verifies its digest. This costs additional disk space, but keeps active jobs and
published playback independent of subsequent overwrite, rename, or deletion.
The source snapshot is reused for later builds of the same bytes.

Explicit upload reclamation is recorded in schema v7's `reclaimed_sources` table.
`POST /api/lessons/<id>/reclaim` requires the displayed completed `build_id`.
Under the shared catalog/ingest lock it checks that this build is ready and
published and rejects linked files. It verifies both files' digests and flushes
the retained snapshot outside the lock, then reacquires it and checks that file
identities and build intent are unchanged. It commits the retained path/digest
before unlinking the upload, then flushes the upload directory. An interruption
before unlink leaves an ordinary upload for the next scan; an interruption after
unlink leaves a durable snapshot-backed lesson. Repeated requests are idempotent.
Each course cleanup sends independent requests sequentially and reports failures
without hiding successful removals. Listing byte counts are file sizes, while the
storage panel reads actual filesystem free space again after cleanup.

Reconciliation includes these lessons even without physical course folders and
still applies settings changes and per-lesson overrides. Reclaimed entries cannot
be mistaken for renamed files when a duplicate video appears elsewhere. A new file
at the same path clears reclamation and is hashed afresh; browser replacement
requires the usual explicit overwrite. Source preparation can copy the recorded
retained snapshot into a different output root for future builds. Existing DBOS
workflow names, step ordering, input specs, and completed history are unchanged.
Saved snapshots and historical media are never garbage-collected by this action;
`site/_sources` must be backed up alongside the catalog once uploads are reclaimed.

```text
library/                         user-managed source videos
state/catalog.sqlite             library metadata, build intent, shared reading state
state/workflows.sqlite           DBOS execution history and checkpoints
state/*.lock                     process/publication coordination
state/worker-results/            remote outputs retained for active builds/replay
work/lessons/<id>/<digest>/       disposable stage caches
site/_sources/<digest>/           immutable source snapshots
site/.staging/<build-id>/         unpublished generated media
site/_revisions/<build-id>/       committed generated media
site/<course>/<lesson-id>.html    atomically replaced public pages
site/status.json                 current worker heartbeat and lesson status
```

Generation never overwrites media used by a published revision. Publication
validates generated references, renames the staging directory into place, commits
the desired lesson record, and atomically replaces public text files. Startup and
ongoing reconciliation repair an interruption between the catalog commit and
page generation. A canceled, removed, or superseded build cannot publish. The
previous published revision remains usable when its replacement fails.

Reading state is namespaced by lesson identity and instruction content. A rename
preserves it; changing instructions gives a new namespace so completion does not
silently attach to unrelated steps. Schema v8 adds `reading_state` (namespace,
lesson ID, step booleans, revision, update time) and `reader_preferences` (one
shared profile, validated preferences and revision). `GET/POST /api/reading`
serves them independently of upload and helper permissions. It uses short SQLite
transactions, without DBOS workflows or the publication file lock.

Every edit validates the currently published namespace and step IDs. Bulk edits
use one `BEGIN IMMEDIATE` transaction and compare each lesson's revision, so one
conflict rolls back the entire batch. Reader preferences use a separate revision.
Undo retains before/after states and revisions in the page and refuses to replace
later edits. Existing browser state is imported only when the namespace has no
server row; resets keep rows so stale browsers cannot resurrect cleared progress.
Historical namespaces stay in SQLite but only current, non-deleted lessons are
returned. Ambiguous old slug-only localStorage keys are not migrated.

`reader-state.js` owns synchronization, validated legacy preference import and
local export fallback; `reading.js` computes the same completion fractions for
all overviews and the reader. Visible pages refresh every ten seconds, on focus,
and after conflicts or uncertain saves. Once a server is detected, an outage
disables edits rather than creating an offline fork. The Settings page and reader
shortcuts share playback preferences. Static exports use localStorage; view and
disclosure preferences stay local in both modes. Separate user identities and an
offline mutation queue are not implemented.

**Library organization**

Schema version 2 adds nullable title overrides to lessons and optional positions
to courses/lessons. Existing databases migrate transactionally. Source filenames
(without extensions, including numeric prefixes) supply default lesson titles;
the generated title becomes the short description. Ordering defaults to natural
source order until explicitly changed. New items append after a custom order.

`GET /api/catalog` returns the editable library and an organization revision.
Processing overrides are separate: `lesson-options:<lesson ID>` in the existing
settings table stores the lesson's `chunk_minutes` override. Reconciliation merges
it with server defaults before calculating the build input key, so a rescan cannot
silently undo a per-lesson edit. No new schema or pipeline version is required.
`GET /api/lessons/<id>/settings` returns the current attempt's values and, when
available, its validated cached probe duration. `POST` to the same endpoint accepts
`build_id` and `chunk_minutes` (null restores the current server default). The API
rejects active/stale attempts and unsupported fields, then commits the override
and new build together under the catalog lock. Existing workflow specs and DBOS
history are never rewritten; upstream caches remain reusable. The queue editor
lives outside the polled job list so polling cannot discard a typed value.

`POST /api/catalog/courses/<id>/title` and
`POST /api/catalog/lessons/<id>/title` update display metadata. A null lesson title
restores its filename. `POST /api/catalog/courses/order` orders courses, while
`POST /api/catalog/courses/<id>/order` orders that course's lessons. These requests
carry the complete ordered `ids` list, or `mode: "source"` to reset ordering, plus
the revision loaded by the client. Missing, duplicate, foreign, and stale items
are rejected before modifying any positions.

The new publication module can render metadata changes in the API process, under
the same catalog lock as the worker. Source files, lesson IDs, URLs, instruction
state, and build intents remain intact. The Library page supports title editing,
up/down controls, moving to a numbered position, order resets, and search. The
course reader uses a vertical list with previous/next navigation.

Schema version 6 adds nullable `lessons.chapter_override`: null uses the naming
heuristic, -1 assigns Other lessons, and 0–999 assigns a chapter. The override is
part of the organization revision and survives reconciliation. It does not enter
build inputs or instruction reading-state keys. Published records and static
exports include the override/effective chapter for consistent navigation.

`POST /api/catalog/courses/<id>/chapter` accepts the selected `ids` and a `chapter`
override. `POST /api/catalog/courses/<id>/move` accepts the selected `ids` and a
`before` lesson ID (null means end). Both require the catalog revision, validate
the entire selection belongs to the course, preserve selected lessons' saved
relative order, and update metadata/positions atomically. Chapter assignments
append to the destination chapter; automatic restoration regroups from names.
Rendering collects all members of a chapter, ordered by first chapter appearance
and saved relative lesson order; Previous/Next uses the same grouped sequence.

**Running locally**

```sh
nix develop

# Terminal 1: durable worker, also creates the initial site.
v2w watch ./library -o ./site --work ./work --state ./.v2w-state --llm heuristic

# Terminal 2: static reader and management endpoints.
v2w serve ./site --library ./library --state ./.v2w-state

# Inspect jobs and use a lesson ID returned by the listing.
v2w jobs list --state ./.v2w-state
v2w jobs retry LESSON_ID --state ./.v2w-state
v2w jobs cancel LESSON_ID --state ./.v2w-state
```

Choose an LLM backend in place of `heuristic` for authored instructions. For a
separate API behind nginx, run
`v2w api ./library --state ./.v2w-state --port 8765`. The NixOS module now runs
`video-to-website` and `video-to-website-api` as separate services so processing
restarts do not terminate uploads. The optional `watch --api-port` convenience
mode still shares a process and does not have that isolation.

The homepage links to `queue.html`, which shows the full queue, positions,
processing stages, search, status filters, and per-lesson retry/cancel controls.
It reads the catalog directly through `GET /api/jobs`, retaining a read-only
`status.json` fallback when the API is unavailable. `GET /api/jobs`
returns the current job projection; `POST /api/lessons/<id>/retry` records a new
build intent and `POST /api/lessons/<id>/cancel` requests cancellation. ffmpeg and
Whisper subprocesses check cancellation while running and terminate their process
group. An in-flight synchronous LLM call may finish before cancellation takes
effect; its result cannot subsequently publish.

**Migration, maintenance, and boundaries**

Upload capacity is managed by `StorageManager`, independently of the lesson
catalog. It accepts named `StorageLocation` roots with free-space buffers. The
current API registers the primary library as `library`; adding actual multi-root
import/routing remains future work. Locations on one filesystem share reservations
and the largest applicable buffer, while separate filesystems have independent
capacity. The storage response is a list of locations plus the selected upload
destination, so the UI does not need to assume a single pooled disk.

`GET /api/storage?course=...` reports disk free/used/total bytes, the buffer, bytes
reserved for incoming data, and currently available upload capacity. The default
buffer is 1 GiB (`--min-free-gib` / NixOS `minFreeGiB`). Every PUT reserves its full
incoming size before reading its body. Already written bytes count as actual disk
usage and are subtracted from the remaining reservation. Replacements reserve the
new file's full size because the old source remains until atomic commit.

Reservation records live under `state/storage/`, or `.storage/` within a standalone
library. Each record is held by an OS file lock for the lifetime of its upload,
and a separate lock serializes capacity checks and writes across API processes.
Closing an upload releases its reservation; after a crash, the next capacity check
reclaims unlocked records and their abandoned temporary files. Active transfers
do not expire based solely on time. Each chunk and final commit recheck free space
in case the worker or another program has consumed it. This is upload admission
control, not a filesystem quota on other writers or future generated artifacts.

Schema version 3 adds a `build_progress` table for live measurements. These are
telemetry inside an existing DBOS step, not new workflow checkpoints. Writers are
throttled to roughly once a second; each stage attempt starts a fresh measurement,
and an old/cancelled stage cannot overwrite current progress. Updating telemetry
does not reset the stage's elapsed time or affect the library-edit revision.

The subprocess wrapper drains stdout and stderr concurrently on the workflow
thread, preserving cancellation and the progress context. Whisper stderr is also
echoed to the journal. ffmpeg uses its `-progress` output; scene metadata streams
through a separate collector so the media clock advances through still passages
without retaining every frame's metadata. Native progress formats are documented
in [ffmpeg](https://ffmpeg.org/ffmpeg.html#Advanced-options) and implemented in the
[Whisper CLI](https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/cli.cpp).

Within a batch, child operations add detail (such as a clip's percentage) while
the parent counts completed transcript sections or illustrated steps. Remaining
time uses elapsed time divided by completed work, after at least two advances and
five seconds of observation. It resets between operations/retries and is withheld
for unknown totals or stale reports. It is a current-operation estimate, not a
prediction for all remaining stages. The frontend shares the same formatting on
the home and queue pages and uses indeterminate bars when no percentage is known.

- The first service scan imports existing files into the catalog. Matching old
  file caches are copied into the UUID cache namespace. Transcript and scene
  caches can be reused; changed dependency keys may require rewriting steps and
  assets. New public URLs use the new identities. Old generated pages are retained
  for manual migration and existing bookmarks; they are not redirected yet.
- Source snapshots and historical generated revisions are retained, including
  after library deletion. There is no automatic garbage collector yet. Deletion
  removes the original library file and current catalog pages; it is not a secure
  erase of every historical copy. The management page explains this. Plan disk
  capacity for snapshots and successive media revisions.
- Back up the original library **and state/**. These databases are no longer
  disposable caches. Stop both services before copying the databases and their
  sidecars, or use SQLite's backup API. Preserve `site/_sources`,
  `site/_revisions`, and `site/.staging` when recovering in-flight workflows:
  checkpoints can refer to those artifacts. If artifacts were deliberately
  discarded, request fresh builds instead of replaying old checkpoints.
- `PIPELINE_VERSION` identifies the workflow step sequence. Keep it stable for
  compatible edits and bump it for incompatible workflow changes. Incompatible
  pending jobs become actionable failures; explicit retry creates a new execution
  with the current code. Use DBOS patching/versioned workers if rolling upgrades
  are needed later.
- This is checkpoint recovery, not transparent process suspension: an interrupted
  Whisper invocation restarts its transcription step. External API calls can repeat
  if a crash happens before their result is checkpointed. Stage cache dependency
  hashes and immutable, idempotent writes remain necessary.
- Uploads remain single-request transfers without resume. Completion uses a unique
  temporary file and a locked destination check, while exact existing-name lookup
  avoids rewriting the target of a rename/delete request. Library reconciliation
  and API mutations share the catalog lock.
- Custom chapter names, moving lessons between courses, trash/restore, resumable
  uploads, and synchronized reading position remain future UI work. Chapter
  assignment, bulk organization, completion and playback settings are built.

Validation includes an actual DBOS worker killed after transcription, removal of
its transcript file cache, and restart against the same SQLite state. The recorded
step is recovered without another transcription call. Other integration checks
cover concurrent upload conflicts, failed replacement publication, source rename,
final deletion, source/configuration changes, API retry/cancel, and subprocess
cancellation. Media/LLM adapters are mocked in the crash tests so they require no
credentials, downloaded models, or paid requests.
