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

**Running as a service.** A NixOS module, an LXC image for Proxmox, nginx in
front, uploads proxied to a loopback port. A dedicated DBOS worker reconciles a
settled library and processes one lesson
workflow at a time. The upload API runs in a separate service.

**Ingest.** Upload videos from the browser, rename them to fix lesson order --
preserving lesson identity and reusable caches -- and delete them.
The home page links to a full task queue with positions, search, status filters,
published-lesson links, retry, and cancel. Generated
media uses immutable revisions, and failed replacements retain the previous page.

## Next

### Stage 3: upload and library management

Keep the existing reader design. Build folder-import preview, upload cancellation
and retry, searchable library management, explicit ordering, chapters, moving
lessons, and bulk actions. Add trash/restore and a retention policy that covers
originals, processing snapshots, historical revisions, and caches separately.
Previous/next lesson links should follow explicit course organization.

The first durable foundation is built: SQLite catalog and schema versioning,
per-lesson DBOS workflows, bounded recovery, immutable publication, separate API
and worker processes, source/configuration invalidation, and exact-name management.
The next architecture work is artifact garbage collection, preserving old URLs
through migration, finer checkpoints for LLM windows/assets, and typed catalog
metadata for chapter/order editing.

### Stage 4: reading state that follows you

Done checkboxes and preferences currently live in `localStorage`. Durable lesson
IDs and instruction-content namespaces now prevent cross-course collisions and
misapplied completion after regeneration. Saved reading position and synchronization
are still to build. Store user reading state in the application SQLite database;
DBOS execution history is separate from that product data.

### Stage 5: per-course tuning

Different courses want different prompts and possibly different models. A
per-course settings file, read at discovery time.

## Smaller things

- **Artifact retention and cleanup.** Immutable source snapshots and old media
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
identity, organization, build intent, and publication records. DBOS owns execution
and recovery. This is a single-host deployment with one worker and local SQLite
files. Both databases are persistent application state and need backups.

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
network, wrong anywhere else. `services.video-to-website.uploads = false` turns
them off and leaves `scp`.

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
