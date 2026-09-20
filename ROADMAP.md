# Roadmap

What is built, what is next, and the decisions that are settled so they do not
get argued out again from scratch. The README describes the tool as it is; this
file is the part that is not true yet.

## Built

**The pipeline.** Transcribe with whisper.cpp, split into steps with an LLM,
cut a screenshot per step and a clip for the steps that are movement, render a
page per lesson with a course index above it. Every stage is cached against the
video's size and modification time, so re-running is cheap and a moved or
renamed file keeps its cache.

**Reading.** Keyboard-driven: step navigation, a floating player that enlarges,
quick seek, playback speed and clip looping that persist, per-step done
checkboxes, Markdown export.

**Running as a service.** A NixOS module, an LXC image for Proxmox, nginx in
front, uploads proxied to a loopback port. The builder watches a library
directory and rebuilds when it settles.

**Ingest.** Upload videos from the browser, rename them to fix lesson order --
carrying the stage cache across so reordering costs nothing -- and delete them.
The home page shows what is being processed, stage by stage.

## Next

### Stage 3: organisation

Chapters are the gap. Videos nested inside a course folder build, but they are
flattened into one lesson list, so a course with sections loses its shape. This
needs a course manifest of some kind, nested directories to mean something, and
prev/next links between lessons -- none of which exist today.

### Stage 4: reading state that follows you

Reading position, done checkboxes and preferences live in `localStorage`, so
they are per browser. Syncing them means a small API and somewhere to keep
them, and this is the one part of the system where SQLite is the right answer
rather than a heavier version of what already works: many small rows, several
writers, real queries, and no sensible representation as files. `sqlite3` is in
the standard library, so it costs no dependency. The work is threading -- the
ingest server already runs alongside the build loop -- and schema migrations.

### Stage 5: per-course tuning

Different courses want different prompts and possibly different models. A
per-course settings file, read at discovery time.

## Smaller things

- **Quarantine a lesson that keeps killing the build.** Whisper on a long
  lesson in a small container gets OOM-killed; systemd restarts the service,
  the watcher rebuilds, and it is killed again. Durable per-lesson attempt
  counts would break that loop. This is the change most likely to matter next.
- **Persist the watcher's state.** `seen` and `done` are in memory, so every
  restart re-walks the library. Cheap with warm caches, but not free.
- **Prune orphaned stage files.** `work/` still holds `whisper.json` and
  `frames.json` from stage names that no longer exist. Nothing cleans them up.
- **Resume a dropped upload.** A drop at 90% starts over. On a LAN that is a
  minute, which is why it is not built.
- **A Blender add-on** showing the steps in the viewport sidebar. Blender can
  do it -- `bpy.data.images.load()` gives a `MOVIE` source, `UILayout` has
  `template_image`, and `WindowManager.event_timer_add` exists -- the only
  catch is that nothing advances the frame on its own. Offered, never asked
  for.

## Settled

**The stage cache stays as files, not a database.** It is write-once,
read-once, keyed by one identity, and measured at roughly 100-200 KB per
lesson. Files give atomicity through rename, `rm -rf work/` as a recovery tool,
and `jq` to see what the model actually returned. More importantly the library
is the source of truth -- everything is derived from `stat()` on the files,
which is why an `scp` and a browser upload are interchangeable. A database
mirroring the filesystem would invent a class of bug that currently cannot
exist.

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
