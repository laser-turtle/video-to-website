# video-to-website

Turn tutorial videos into a static website you can read at your own pace: condensed
steps, a screenshot of the expected result after each step, and a click on any step
jumps the embedded video to that moment.

Built for screen-recorded course videos (Blender, DAWs, IDEs, CAD). Nothing is
uploaded except the transcript text, and even that is optional.

## What it produces

For every video, one page containing:

- a short summary and any prerequisites
- numbered steps, each leading with its clip or first screenshot and carrying the
  imperative actions, keyboard shortcuts and gotchas directly underneath, so that what
  to do sits beside the picture you are matching rather than a scroll above it
- a screenshot taken at the moment the step's result is on screen
- a timestamp button that seeks the embedded player, so you can watch just that part
- a per-step "done" checkbox that persists in the browser
- the full transcript, collapsed, with clickable timestamps

Plus a course index, a top-level index across courses, and a Markdown export of every
lesson.

## Quick start

There is a Makefile wrapping the longer commands; `make` on its own lists them.

```bash
make build COURSE=~/courses/blender-donut   # build a site
make serve                                  # look at it
make test                                   # both test suites
make deploy HOST=lessons                    # push it to the server
```

Or drive it directly:

```bash
# from this directory, no install
nix run . -- build ~/courses/blender-donut -o site

# or drop into a dev shell
nix develop
v2w doctor                    # check ffmpeg, whisper, credentials
v2w fetch-model small.en      # ~500 MB, once
v2w build ~/courses/blender-donut -o site
v2w serve site                # http://127.0.0.1:8000, supports video seeking
```

`nix run .#v2w-with-small-en` uses a whisper model pinned in the flake, so nothing is
downloaded at runtime.

## How it works

Each stage caches to disk, keyed on the source file plus that stage's settings. Re-runs
only redo what changed, which matters because transcription is the slow part.

| Stage | Tool | Output |
|---|---|---|
| probe | ffprobe | duration, resolution, codecs |
| transcribe | whisper.cpp | timestamped transcript segments |
| scenes | ffmpeg scene filter | timestamps where the screen changed |
| steps | Claude, Ollama, or no LLM | titles, actions, shortcuts, time ranges |
| frames | ffmpeg | one screenshot per step |
| render | — | HTML, CSS, JS, Markdown |

Step screenshots are taken at the last visual change inside a step's time range, which
is usually the moment its result is on screen.

## Choosing how steps get written

```bash
v2w build COURSE --llm claude-cli    # Claude subscription via the `claude` CLI, no API key
v2w build COURSE --llm anthropic     # Claude via ANTHROPIC_API_KEY
v2w build COURSE --llm ollama        # local model, nothing leaves the machine
v2w build COURSE --llm heuristic     # no model at all: mechanical segmentation
v2w build COURSE --llm codex-cli     # Codex subscription (untested)
```

`--llm auto` (the default) prefers `ANTHROPIC_API_KEY` when set, then the `claude` CLI,
then a reachable Ollama.

The `claude-cli` backend shells out to Claude Code in print mode, so a Claude
subscription works with no API key. Pick the model with `--llm-model opus|sonnet` or
`V2W_CLAUDE_MODEL`. Note that it deliberately does not pass `--bare`: that flag skips
keychain reads, which is where a subscription login lives.

Only transcript text is sent, never audio or video. Roughly 8k tokens go out per
30-minute lesson. Server-side refusal fallbacks are enabled by default on the
`anthropic` backend; turn them off with `--no-llm-fallbacks`.

For Ollama, set the model with `--llm-model qwen2.5:14b` or `V2W_OLLAMA_MODEL`. A 14B
model is about the floor for usable step extraction.

## Screenshots

Each step gets up to four screenshots rather than one, so a step shows its own progress
instead of jumping from start to finished state. Moments are chosen from the scene
scores inside the step: the final state is always captured, and the rest are the
highest-scoring changes spaced at least a few seconds apart. Longer steps earn more
shots, short ones stay at a single image.

Two scene changes often show the same screen, so each candidate is reduced to a 64-bit
perceptual hash and dropped when it is within `--frame-dedup-distance` of one already
chosen. On real Blender footage, genuinely different screens sit 11 bits apart or more,
while frames under a second apart sit at 0 to 6, so the default of 8 falls in the gap.

Every screenshot is clickable and seeks the player to its own timestamp, not the step's
start. Reading the small print in one is a different intent, so it does not compete for
that click: each screenshot carries an enlarge control in its corner, and `z` enlarges
the one belonging to the current step. The enlarged view shows the image at full size
over a dark backdrop with a "play from" button, so the jump is still one click away;
`Esc` or a click on the picture closes it.

## Clips for steps that are movement

A still cannot show a drag, an extrude, or a loop cut being slid into place. The model
marks those steps, and each one gets a silent clip that loops in place.

A clip covers its whole step rather than a fixed few seconds. Step boundaries come from
whisper's timestamps and steps tile the lesson end to end, so a clip runs from its
step's start to its step's end, plus half a second of lead-in and a second of tail. A
fixed length stopped wherever it stopped and stranded whatever happened just after,
because the next step's clip had already moved past it, leaving the original video as
the only way to see the join. A clip is also extended to the end of a sentence that
straddles the boundary, so it never cuts mid-word.

Clips run to `--clip-max-seconds` at most, 45 by default. On the sample lessons that
truncates nothing; at 30 seconds it truncated nearly half of one lesson's clips, which
is the failure it exists to avoid. A truncated clip keeps its tail, where the step's
result appears.

A clip covers a whole step, and a step usually contains stretches where the instructor
is talking and the screen does not move. Those seconds earn their place in the lesson
video, which has the audio, but in a silent looping clip they are dead air. Each
motionless run is therefore cut back to `--clip-max-still` seconds, 1.5 by default, and
what survives is re-timed so the clip plays continuously. On one sample step this turned
19 seconds with an 8.7 second pause in the middle into 11.5 seconds of actual work. The
caption says so when a clip has been tightened; `--clip-max-still 0` turns it off.

Stillness is detected by sampling the window at 10 frames per second and scoring how
much changes between samples. The sampling rate matters: these sources run at 60fps,
where consecutive frames differ so little during a slow drag that real movement scores
the same as a frozen screen. Comparing frames 100ms apart separates the two cleanly.
That rate is for detection only and has nothing to do with playback.

Clips are h264 MP4 in a muted looping `<video>`, not GIF: a GIF of the same thing is
more than twice the size at two thirds the resolution. They are encoded at
`--clip-fps`, 30 by default. Frame rate turns out to be nearly free, because h264
spends almost nothing on near-identical frames: on a sample clip 30fps cost 13% more
bytes than 15fps and 60fps cost 16%, so smoothness is worth paying for.

Screenshots and clips are both produced at 1920px wide by default, and neither is ever
upscaled past its source. Quality costs real bytes here, unlike frame rate: 1080p
screenshots are about 1.8 times the size of 720p ones, and 1080p clips several times
the size of 960px ones. `--frame-width`, `--frame-quality`, `--clip-width` and
`--clip-crf` dial it back. Page weight stays manageable regardless, because screenshots
load lazily and only the current step's clip is ever fetched.

A clip runs while its own step is the current one and that step is on screen, so at most
one clip is ever moving and it is the one belonging to the step you are working on.
Moving to another step stops it and rewinds it, so coming back shows the action from the
start rather than halfway through a loop. Clicking a clip pauses it so you can study a
frame, and it stays paused until you click again, even if you move away and back. A progress bar along the bottom shows where the
loop is; drag it to scrub, which also holds the frame still. `r`, or the control next to
the play button, decides whether clips loop or play once; that control is the indicator
too, lit when looping is on. Like the speed, the choice is remembered across lessons. The timestamp in the
caption is a button that jumps the main player to that moment.

A step with a clip keeps a single screenshot rather than several. The clip already
shows the span moving, so more stills of it are the same information twice, but one is
still worth having: it is the checkpoint you compare your own screen against, and it
reads without waiting for a loop.

```bash
v2w build COURSE --clips auto         # only steps the model marks as movement (default)
v2w build COURSE --clips all          # every step
v2w build COURSE --clips none         # stills only
v2w build COURSE --clip-max-seconds 60
v2w build COURSE --shots-with-clip all   # keep every screenshot alongside a clip
```

When the model does not label a step, a keyword scan over the step's own wording decides
(drag, slide, extrude, scale, rotate, bevel, loop cut, and similar).

## Reading a lesson page

The steps and their screenshots get the full column, because that is what you read. The
video is reference material, so it lives in a small player floating in the corner that
starts collapsed and opens by itself the moment you click a timestamp. Its header
collapses it again, and that choice is remembered.

Playback speed steps through 0.75x up to 3x with `,` and `.`, and is remembered under a
key of its own rather than per lesson, so the speed you watch at follows you from one
lesson to the next. Clips run at it too. Changing it with the player's own controls is
picked up as well, and the current speed sits in the player's header.

The player has three sizes. Collapsed it is a chip showing the current time. Open it
sits in the corner. Pressing <kbd>f</kbd>, or the button in its header, moves it to the
centre of the screen at full size for studying one passage closely; <kbd>f</kbd> again,
<kbd>Esc</kbd>, or a click outside returns it to the corner. The larger view is
deliberately not remembered, because it is meant to be temporary.

One step at a time is the cursor, highlighted and moved with `j` and `k`. A step
carrying a clip and a screenshot is often taller than the window, so `j` pages down
through the step it is on and only moves to the next one once you have reached its end;
`k` is the exact inverse, landing on the previous step's last page rather than its
start. Arriving at a step aligns its top, not its middle, because the text worth reading
is at the top. Scrolling by hand is respected: the next `j` resumes from whatever is
actually on screen. The page animates its own scrolling over 200ms, because
the browser's built-in smooth scrolling has a fixed duration that is slow enough to get
in the way; `SCROLL_MS` in `render.py` is the dial.

`Enter` marks the step done and moves on, which is the whole loop when you are working
along in another window: read, do it, `Enter`, read. `Home` returns to the top, where
the summary and prerequisites live, and `End` jumps to the last step. `g` plays the
video from the current step, `c` plays its clip, `Space` and the arrow keys drive
playback, `,` and `.` change speed, `h` and `l` seek ten seconds either way, `[` and `]` nudge the
current step's clip by two, stopping at either end and only coming round to the other
side if pressed again there, and `?` lists the lot. Playback
moves the cursor too, but never scrolls the page under you.

Every step timestamp, screenshot, clip caption, and transcript line is a jump point.
Clicking the same one a second time pauses, so a click is how you stop to look at
something and a second click is how you carry on. Each step has a checkbox that
persists in the browser, and the step covering the current playback position is
highlighted as the video plays.

Screenshots and clips declare their dimensions, so the page does not reflow under you as
they load.

## Ads and promos

Instructors advertise their own courses mid-lesson, and those segments should not become
steps. A keyword scan flags likely promotional stretches ("link in the description",
"my free starter course", Patreon, subscribe appeals). The model receives those ranges
as a hint and decides for itself, returning the ones it agrees with; any step sitting
mostly inside a skipped range is dropped, and the lesson page lists what was left out.

The `heuristic` backend has no judgement, so it applies the keyword scan directly.

## Whisper models

```bash
v2w fetch-model --list
v2w fetch-model small.en          # good default for clear narration
v2w build COURSE --model medium.en
v2w build COURSE --model /path/to/ggml-large-v3-turbo.bin
```

`base.en` and `small.en` are pinned by hash in `flake.nix` and available as
`nix build .#whisper-models-small-en`. Other models download at runtime into
`$XDG_DATA_HOME/video-to-website/models` (override with `V2W_MODEL_DIR`).

To pin another model in the flake, get its hash and add it to `modelHashes`:

```bash
nix store prefetch-file https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin
```

## Running it as a service

There is a NixOS module and a Proxmox LXC image in the flake. The service
watches a library directory and rebuilds whenever it settles after a change, so
adding a lesson means dropping a file in.

```
/var/lib/video-to-website/
  library/<course>/<lesson>.mp4   drop videos here
  site/                           generated, served by nginx
  work/                           stage cache, safe to delete
```

A change is only acted on once the library has looked the same for a full poll,
so a half-copied file is never built. Rebuilds are cheap because every stage is
already cached on the source file.

### Getting it onto Proxmox

Proxmox does not ship a NixOS container template, and the image is an
`x86_64-linux` closure, so a Mac cannot build one. The Proxmox host itself is
Debian on x86_64, which makes it the easiest builder you already own.

**First**, put your SSH public key in `nix/lxc.nix` and commit it. A flake only
sees files git knows about, so an uncommitted key is an invisible one.

**On the Proxmox host**, install Nix and build the template:

```bash
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
git clone <your-remote> video-to-website && cd video-to-website
nix build .#packages.x86_64-linux.lxc-image
cp result/tarball/*.tar.xz /var/lib/vz/template/cache/nixos-lessons.tar.xz
```

**Create the container.** `unmanaged` matters: it stops Proxmox trying to
rewrite the `/etc` files NixOS owns.

```bash
pct create 200 local:vztmpl/nixos-lessons.tar.xz \
  --ostype unmanaged --hostname lessons \
  --cores 4 --memory 4096 --rootfs local-lvm:200 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --features nesting=1 --unprivileged 1
pct start 200
pct exec 200 -- ip -4 -brief addr show eth0
```

Give the rootfs room for the videos, or bind-mount a dataset at
`/var/lib/video-to-website/library`. A bind mount into an unprivileged
container needs the uid mapping to line up, so the simple rootfs is the quieter
option to start with.

**Networking is owned by the flake, not by Proxmox.** The `proxmox-lxc` module
defaults to letting Proxmox configure the container's network and hostname, but
`--ostype unmanaged` above is exactly what stops Proxmox writing into the
guest's `/etc`. Left alone, the two cancel out and the container boots with no
IP address at all. `nix/lxc.nix` therefore sets `proxmoxLXC.manageNetwork` and
`manageHostName` to true and declares the address itself -- **edit the address,
gateway and nameserver there to match your LAN before building the image.** An
IP set with `pct set ... -net0 ip=...` is ignored.

If a container is already running with no address, configure it by hand just
long enough to deploy the real config over it:

```bash
pct exec 200 -- /bin/sh -lc 'ip link set eth0 up
  ip addr add 192.168.1.202/24 dev eth0
  ip route add default via 192.168.1.1
  echo nameserver 192.168.1.1 > /etc/resolv.conf'
```

That survives until the next reboot, which is long enough for `make deploy` to
make it permanent.

**There is no console login.** The container has no root password, so the
Proxmox console sits at a prompt nothing will satisfy. That is deliberate --
the way in is from the host, which needs no password at all:

```bash
pct enter 200            # root shell, no login
```

`pct enter` starts a login shell, so PATH is correct inside it. `pct exec`
does not -- see the note below.

SSH is the other way in, using the key you committed in step one. Add a console
password only if you want one; `nix/lxc.nix` has the line and the caveat.

**If SSH rejects your key**, the image was almost certainly built before the key
was added to `nix/lxc.nix` -- the keys are baked in at build time, so a template
built from an older commit accepts nothing. Rather than rebuilding the template,
put the key in the one file the config does not manage:

```bash
pct exec 200 -- /bin/sh -lc 'install -d -m700 /root/.ssh'
pct exec 200 -- /bin/sh -lc 'cat >> /root/.ssh/authorized_keys' < ~/.ssh/id_x.pub
pct exec 200 -- /bin/sh -lc 'chmod 600 /root/.ssh/authorized_keys'
```

sshd reads `%h/.ssh/authorized_keys` *and* `/etc/ssh/authorized_keys.d/%u`. The
declarative keys go in the second, so this one is not overwritten by a deploy.
Once `make deploy` lands, the key in `nix/lxc.nix` takes over and this copy is
just redundant.

**Put the API key on it**, outside the Nix store. This does not need SSH to be
working yet.

`pct exec` runs its command with a stock `/bin:/usr/bin` PATH, and NixOS has
neither directory -- `pct exec 200 -- install ...` fails with `Failed to exec
"install"`. Go through a login shell so `/etc/profile` sets the real PATH:

```bash
pct exec 200 -- /bin/sh -lc 'install -d -m700 /var/lib/secrets'
pct exec 200 -- /bin/sh -lc 'umask 077; echo ANTHROPIC_API_KEY=sk-ant-... > /var/lib/secrets/v2w.env'
pct exec 200 -- /bin/sh -lc 'ls -l /var/lib/secrets/v2w.env'
```

`/bin/sh` is the one path NixOS does provide. The same `-lc` wrapper is what
any later `pct exec` needs.

`nix/lxc.nix` already points `environmentFile` at that path. Now deploy from
your Mac -- the container builds its own closure, so your machine only
evaluates:

```bash
make deploy HOST=<ip>
```

`nixos-rebuild` ships with NixOS, so it is not on a Mac. The Makefile runs it
out of the flake's own nixpkgs with `nix run --inputs-from .`, so there is
nothing to install; override `NIXOS_REBUILD` if you have your own.

That is also how every later change goes out.

**Add a course**, either from the browser -- *Add videos* at the top of the
home page -- or over ssh:

```bash
scp -r ~/courses/blender-donut root@<ip>:/var/lib/video-to-website/library/
ssh root@<ip> journalctl -fu video-to-website
```

Then open `http://<ip>/`.

**If the page is 403 Forbidden**, the site directory is almost certainly still
empty. nginx answers a directory with no index file and no autoindex with 403,
not 404, so an unbuilt site looks exactly like a permissions failure. A first
build takes a while -- the library has to stay unchanged for a full poll before
the service touches it, then every video gets transcribed -- so check the log
rather than the page:

```bash
ssh root@<ip> 'ls -la /var/lib/video-to-website/site'
ssh root@<ip> journalctl -u video-to-website -n 50
```

The service now writes a placeholder index at startup, so a box running a build
of this or later answers with a page saying as much. If you still get 403 with
an `index.html` sitting in that directory, then it really is permissions: nginx
has to be able to traverse `/var/lib/video-to-website` and read `site/`.

**Watching a build from the page.** The builder keeps `status.json` next to the
site, and the index page polls it every few seconds: each video shows the stage
it is in, how long it has been there, and a bar across the five per-video
stages. Copy a file in and it appears within a poll, first as waiting for the
copy to settle, then working through probe, transcribe, scenes, steps and
frames. When a build finishes the page reloads itself, so a course that has
just finished turns up without a manual refresh.

Whisper is most of the wall time, so a lesson sits on *Transcribing the audio*
for minutes. That is normal; `journalctl -fu video-to-website` has the detail if
you want it.

### Uploading from a browser

*Add videos* on the home page takes a course name and however many files, and
PUTs them into the library one at a time with a progress bar each. They land as
ordinary files, so the watcher picks them up exactly as it would an `scp` --
there is no separate queue, and nothing to go wrong between the two.

A file is written under a leading dot while it arrives and renamed into place
when it finishes, so a half-uploaded video can never start a build of itself. An
upload onto an existing name is refused rather than guessed about; the page
offers to replace it.

The service listens on loopback only and nginx proxies `/api/` to it, with body
buffering off so a 2 GB lesson streams through rather than filling nginx's
spool first.

**There is no authentication.** Anyone who can reach the site can add a course
or replace a lesson. That is the right trade on a home network and the wrong one
anywhere else -- set `services.video-to-website.uploads = false` to turn the
endpoint off and keep `scp`, or put the whole vhost behind auth.

## Where the state lives

Nothing is in a database, and nothing lives outside these two directories:

```
<stateDir>/library/                 the videos, exactly as you put them there
<stateDir>/work/<course>/<lesson>/  the stage cache: probe, transcript,
                                    scenes, steps, assets -- one JSON each
<stateDir>/site/                    everything served, all of it generated
<stateDir>/site/site.json           every course, lesson and step as data
<stateDir>/site/status.json         what the builder is doing right now
```

`work/` is a cache: deleting it costs a re-transcription and nothing else.
`site/` is output: deleting it costs a rebuild, which is cheap while `work/` is
intact. The library is the only thing that is not reproducible, so it is the
only thing to back up.

Course and lesson names come from the directory and file names, read fresh on
every build -- there is no stored title to get out of step with them. Renaming
a course folder renames it on the site at the next build, and the old pages are
removed with it.

## What happens when things go wrong

The design principle is that a video is only ever built from a file that is
completely on disk, and that no failure is allowed to leave a half-written page
where a whole one used to be.

**A copy that is still in progress.** The watcher waits for the library to look
identical for a full poll before it touches anything, so a file still being
written is not picked up. Browser uploads go further: they arrive under a
leading dot, which `find_videos` skips, and are renamed into place only when
complete.

**A truncated or corrupt video.** It fails at the probe stage, is logged, shows
as *Failed* on the home page, and the rest of the course builds around it.
Replacing the file changes its size or modification time, which is a library
change, which starts a fresh build.

**A build that dies part way.** Each stage writes its cache atomically and only
once it has finished, so a restart resumes from the last completed stage rather
than from the beginning. Screenshots and clips are re-cut whenever their stage
is re-run, so a half-written JPEG from a killed ffmpeg is overwritten rather
than kept.

**A deploy in the middle of a build.** `nixos-rebuild` restarts the service,
which is the case above. The page reloads itself only when a build *finishes*,
so an interrupted one leaves open browsers alone.

**A build that fails outright** -- the API refusing, the disk full -- keeps the
last good site exactly as it was, says so on the home page, and retries with a
doubling delay up to an hour rather than waiting for someone to notice. The
stage cache means a retry resumes rather than starting over. Touching the
library resets the backoff.

**An upload that drops part way** leaves nothing behind and has to be started
again; there is no resume. On a LAN that is a minute of lost time, which is why
it has not been built.

**The API key** goes in a file on the server rather than the Nix store, which is
world readable:

```nix
services.video-to-website = {
  enable = true;
  environmentFile = "/var/lib/secrets/v2w.env";   # ANTHROPIC_API_KEY=sk-ant-...
  llm = "anthropic";
  llmModel = "claude-sonnet-5";
};
```

The API rather than the CLI is the default for the service on purpose: a box
working through a course wants predictable per-token billing, not a session
quota it can exhaust. Sonnet is the default model for the same reason; raise
individual lessons to Opus if their coverage looks thin.

The whisper model is pinned by hash and baked into the closure, so the service
never downloads weights at runtime. `services.video-to-website.whisperModels`
changes which, or set it to null to let it fetch its own.

## Running it on a Linux server

The flake builds for `x86_64-linux` and `aarch64-linux` as well as macOS. On a box with
an NVIDIA card, `nix build .#v2w-cuda` builds whisper.cpp with CUDA, which is several
times faster than CPU transcription. It is unfree and compiles from source, so the first
build takes a while.

Transcription is the only heavy stage. A reasonable split is to build on the server and
serve the output directory with any static file server:

```bash
v2w build /srv/courses -o /srv/www/courses
```

Source videos are symlinked into the site as `videos/<slug>.<ext>`. Use `--videos copy`
if your web server will not follow symlinks, or `--videos none` to leave videos out
entirely (timestamps then render as plain labels).

## Useful options

| Option | Why you would change it |
|---|---|
| `--scene-threshold 0.02` | Too few screenshots. Lower finds more changes; re-running is cheap because scores are cached. |
| `--chunk-minutes 15` | Very long lessons, or a model with a smaller context window. |
| `--frame-width 1600` | Small UI text in screenshots. |
| `--frames-per-step 6` | Steps still feel like they skip work. |
| `--frame-dedup-distance 4` | Screenshots look repetitive; raise it to allow more similar shots. |
| `--screenshot-every 10` | Steps still skip work between shots. |
| `--clips all` | You want every step to move, not just the ones marked motion. |
| `--clip-max-seconds 20` | Clips feel too long, or the site is too heavy. |
| `--clip-max-still 0.8` | Clips still have dead air in them. |
| `--clip-fps 60` | You want clips as smooth as the source. |
| `--frame-width 1280` | Screenshots are taking too much disk. |
| `--clip-crf 28 --clip-width 1280` | Clips are taking too much disk. |
| `--shots-with-clip all` | You want the stills back on steps that also have a clip. |
| `--force steps` | Re-run one stage after changing the prompt or backend. |
| `--stop-after transcribe` | Just get transcripts, skip the rest. |
| `--videos copy` | Site needs to be self-contained or served without symlinks. |
| `--keep-audio` | Debugging transcription quality. |

Stages for `--force` and `--stop-after`: `probe`, `transcribe`, `scenes`, `steps`,
`frames`, `render`. `--force all` rebuilds everything.

## Course layout

Point `v2w build` at a folder. If that folder holds videos, it is one course. If it
holds subfolders that hold videos, each subfolder becomes its own course. Files are
ordered naturally, so `lesson 2` sorts before `lesson 10`.

That descent is **one level only**, which matters for the service: the library is
itself the folder of course folders, so a course goes directly inside it.

```
/var/lib/video-to-website/library/
  cgboost_launch_pad_2/          <- a course
    01_car_body.mp4              <- its lessons
    02_wheels.mp4
```

Nest one deeper -- `library/courses/cgboost_launch_pad_2/` -- and the wrapper
becomes the course instead: a single course named `courses` with every video
under it flattened into one lesson list. Moving the course folder up one level
fixes it, and costs no rebuilding: the stage cache identifies a video by its
size and modification time rather than its path, so a course folder can be
renamed or moved without re-transcribing it. Move its directory under `work/`
to match the new course name and even that lookup stays warm.

```
site/
  index.html                        all courses
  <course>/index.html               lesson list
  <course>/<lesson>.html            the guide
  <course>/frames/<lesson>/*.jpg    step screenshots
  <course>/clips/<lesson>/*.mp4     short looping clips for motion steps
  <course>/videos/<lesson>.mp4      symlink to the source video
  <course>/md/<lesson>.md           markdown export
  .work/                            stage cache, safe to delete
```

## Requirements

`ffmpeg` and `whisper-cli` on PATH. Both come from the flake. `v2w doctor` reports what
is missing.
