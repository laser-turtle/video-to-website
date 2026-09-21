# Roadmap

What is built, what is next, and the decisions that are settled so they do not
get argued out again from scratch. The README describes the tool as it is; this
file is the part that is not true yet.

## Built

**The pipeline.** Transcribe with whisper.cpp, split into steps with an LLM,
cut a screenshot per step and a clip for the steps that are movement, render a
page per lesson with a course index above it. Stages retain file caches; the
service also checkpoints each stage with DBOS.
A SQLite catalog tracks stable lesson IDs, source digests, desired builds, and
published revisions. See [ARCHITECTURE.md](ARCHITECTURE.md).

**Reading.** Keyboard-driven: step navigation, a floating player that enlarges,
quick seek, playback speed and clip looping that persist, per-step done
checkboxes, Markdown export.
Tall-step paging aligns the instructions below the first visual at the top.
Shift+J/K moves between lessons, Shift+L/H between chapters, and `/` opens a
keyboard-operated lesson picker with partial-name search.
Introductions/overviews without extracted steps publish as video lessons with an
inline player, poster, and available summary/transcript. Silent/empty-transcript
videos use the same fallback; genuine processing errors remain failures.

**Running as a service.** A NixOS module, an LXC image for Proxmox, nginx in
front, uploads proxied to a loopback port. One DBOS coordinator reconciles the
library and runs bounded lesson workflows. Optional helpers add processing
capacity; the API runs in a separate service.

**Library.** A searchable Library page with course/lesson title editing, saved
reading order, up/down and move-to-position controls, and order resets. Filenames
(including numbering) are lesson titles; generated titles are short descriptions.
Course lessons list vertically, and previous/next links follow the grouped reading order.
Metadata edits publish without processing videos or changing filenames.
Course and lesson navigation infer collapsible chapters from leading title/source
numbering. Search, expand/collapse, compact rows and temporary number/title/duration
sorts work on static exports and remember view preferences per browser. The reader
contents menu highlights the current lesson. Each chapter collects all its lessons
into one group; Previous/Next follows that grouped sequence.
The Library management page has the same chapter/compact views and can save a
course's reading order using smart numbering, title, filename or duration, plus
sort courses by name. Sorts include pending lessons, use revision checks, and
publish immediately without creating processing jobs.
Bulk selection supports whole chapters, search matches, and individual lessons.
Selected lessons can move together in saved order or be assigned to an existing/
new chapter, with an option to restore automatic grouping. Chapter overrides are
durable display metadata in catalog schema v6.

**Ingest.** Upload videos from the browser, rename source files, and delete them.
Reclaim redundant uploads per lesson or course after checksum verification,
retaining published lessons and snapshot-backed reprocessing across restarts
(catalog schema v7). Reclaimed originals stay visible and replacement is explicit.
The library and upload pages show disk capacity and a configurable free-space
buffer. Admission reserves space across concurrent uploads, rejects insufficient
capacity, and releases abandoned reservations after crashes. Storage accounting
uses named locations and shared filesystem capacity in preparation for multiple
library roots; multi-root import and destination selection remain to build.
The home page links to a full task queue with positions, search, status filters,
and per-course filtering. Course cards show only compact activity counts;
individual task details and controls live on the queue page. It supports
published-lesson links, retry, and cancel. Generated
media uses immutable revisions, and failed replacements retain the previous page.

**Progress.** Live Whisper percentages, ffmpeg media-time progress, transcript
section counts, streaming model activity, and per-step screenshot/clip progress.
Scoped time estimates use observed work rates and pause when reports go stale;
unknown work stays indeterminate. Journal output is retained.

**Provider funding and instruction recovery.** Credit/billing/spend-limit failures
pause that backend's requests, with durable pause state and Resume requests in
the queue. Waiting remains cancellable and survives deployments. Accepted LLM
sections are checkpointed inside the existing instruction stage; old workflows
and full-stage caches remain valid. Automatic balance checking, multiple
credential scopes, and provider-specific scheduling remain future work.

**Processing settings.** The queue exposes per-lesson transcript section length,
including a smaller-section suggestion after `max_tokens` failures and Save and
retry. Overrides persist across rescans and restarts; restoring the server default
is explicit. Changes create a new attempt and preserve existing published content.

**Distributed processing.** Setup and protocol details are in
[DISTRIBUTED_WORKERS.md](DISTRIBUTED_WORKERS.md). Helpers can join and leave, with
fenced assignments and automatic local fallback. The Workers page provides
pairing, availability, current work, pause/resume/stop/revoke, naming and durable
contribution history. SQLite, DBOS and the authoritative library stay on the server.
The connection page serves a matching, self-contained Python helper download,
checks native prerequisites, and generates the pairing command after setup.
Saved helper identities survive fresh pairing codes. Old workers can be archived,
restored, or permanently removed together with their contribution history, subject
to preserving results required for unfinished lessons.
Media generation now supplies a bounded batch of independent galleries and clips
from one lesson. Faster helpers can keep claiming work while other computers are
busy; lesson order, screenshot deduplication, recovery and CPU limits are preserved.

## Next

Follow-ups for helpers are RTX 3090/M3 benchmarking, installers and login-service
integration, battery/idle schedules, resumable transfers, and an Apple
VideoToolbox clip profile.

### Stage 3: upload and library management

Keep the existing reader design. Build folder-import preview, upload cancellation
and retry, custom chapter names, moving lessons between courses, and bulk deletion. Add
trash/restore and a retention policy that covers
originals, processing snapshots, historical revisions, and caches separately.
Title editing, ordering, automatic chapter grouping, and previous/next navigation are now built.

The first durable foundation is built: SQLite catalog and schema versioning,
per-lesson DBOS workflows, bounded recovery, immutable publication, separate API
and worker processes, source/configuration invalidation, and exact-name management.
The next architecture work is artifact garbage collection, preserving old URLs
through migration and finer media checkpoints. LLM section checkpoints are built.
Display title and order metadata are now persisted in schema v2.

### Stage 4: reading state that follows you

Done checkboxes and preferences currently live in `localStorage`. Durable lesson
IDs and instruction-content namespaces now prevent cross-course collisions and
misapplied completion after regeneration. Saved reading position and synchronization
are still to build. Store user reading state in the application SQLite database;
DBOS execution history is separate from that product data.

### Stage 5: per-course tuning

Different courses want different prompts and possibly different models. A
per-course settings file, read at discovery time.
Per-lesson section length is already editable from the queue. Per-course defaults,
model selection and prompt editing remain to build.

## Smaller things

- **Benchmark existing processing hardware.** Try the RTX 3090 and M3 helpers
  before deciding whether the server also needs a permanent GPU. Earlier server
  hardware notes remain in [GPU_UPGRADE.md](GPU_UPGRADE.md).
- **Artifact retention and cleanup.** Explicit duplicate-upload reclamation is
  built. Immutable source snapshots and old media
  revisions are retained today. Garbage collection must respect published and
  in-flight workflow references. Raw `whisper.json` is still an intentional output.
- **Resume a dropped upload.** A drop at 90% starts over. Add resumable transfer
  after basic queue cancellation/retry, guided by actual file sizes and failures.
- **A Blender add-on** showing the steps in the viewport sidebar. Blender can
  do it -- `bpy.data.images.load()` gives a `MOVIE` source, `UILayout` has
  `template_image`, and `WindowManager.event_timer_add` exists -- the only
  catch is that nothing advances the frame on its own. Offered, never asked
  for.

## Settled

**Python + SQLite + DBOS for the durable service.** The application catalog owns
identity, organization, build intent, publication and native-task assignments.
DBOS owns lesson workflow execution and recovery on one server. Optional helpers
communicate through the API and never open either local SQLite database. Both
databases are persistent application state and need backups.

**Stage caches and media stay as files.** They remain inspectable and reusable.
The catalog does not store video bytes. Source snapshots and published media are
immutable; public pages are replaced atomically. Filenames and display names do
not serve as lesson identity.

**The API, not the CLI, for the server.** A box working through a course wants
predictable per-token billing, not a subscription quota it can exhaust.
Measured from real transcripts at about 270 input and 300 output tokens per
minute of video: Opus $0.54, Sonnet $0.21, Haiku $0.11 per hour of video.
Dollars are not the constraint; rate limits were. Sonnet is the default.

**No authentication on the upload and management endpoints.** Right on a home
network, wrong anywhere else. `services.video-to-website.uploads = false` disables
library mutations and leaves `scp`. The separate `workers` option controls helper
and worker-management endpoints; disable both to turn off all management APIs.

**The service owns its library.** It takes ownership at start, because videos
arrive by `scp` as root as often as through the page, and a root-owned course
folder is one the builder can read but not write.

## Dead ends

**Downloading courses with yt-dlp.** The Teachable extractor is marked
`_WORKING = False` and only auto-matches eight hardcoded domains. Both the
`teachable:` prefix and `--impersonate chrome` returned 403. Uploading cookies
would not change it. Download in a browser, upload through the page.

**GPU transcription on the spare card.** nixpkgs builds CUDA for compute
capability 7.5 and up; a GTX 660 is 3.0.
