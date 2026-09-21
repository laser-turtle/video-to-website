# Shortcuts for the long commands. `make` on its own lists them.

COURSE ?= samples
OUT    ?= site
PORT   ?= 8000
HOST   ?= 192.168.1.201
SYSTEM ?= x86_64-linux
LLM    ?= claude-cli

# make treats an unescaped # as a comment, even mid-value, which silently
# truncates every flake reference below. Keep the indirection.
HASH := \#

V2W := nix run .$(HASH)v2w --

# nixos-rebuild ships with NixOS, so it is missing on a Mac and on non-NixOS
# Linux. --inputs-from pins it to the same nixpkgs as the rest of the flake.
NIXOS_REBUILD ?= nix run --inputs-from . nixpkgs$(HASH)nixos-rebuild --

.PHONY: help dev test check build rebuild serve doctor fmt lxc-image deploy deploy-dry clean clean-site

help:
	@echo "Targets:"
	@echo "  make test                 run the python and browser-script tests"
	@echo "  make check                nix flake check (builds the package too)"
	@echo "  make build                build a site      [COURSE=$(COURSE) OUT=$(OUT) LLM=$(LLM)]"
	@echo "  make rebuild              build, re-running every stage"
	@echo "  make serve                serve it          [OUT=$(OUT) PORT=$(PORT)]"
	@echo "  make doctor               check ffmpeg, whisper and credentials"
	@echo "  make dev                  enter the dev shell"
	@echo "  make fmt                  format the nix files"
	@echo "  make lxc-image            build the Proxmox container image (needs a Linux builder)"
	@echo "  make deploy               deploy to the server  [HOST=$(HOST)]"
	@echo "  make deploy-dry           show what deploying would change"
	@echo "  make clean-site           delete the generated site, keep the caches"
	@echo "  make clean                delete the site and every cache"

dev:
	nix develop

# The flake check runs these too, in a sandbox; this is the fast loop.
test:
	nix develop -c sh -c 'PYTHONPATH=src python -m unittest discover -s tests -q \
	  && python -c "from video_to_website import render; [open(n,\"w\").write(getattr(render,a)) for n,a in [(\"app.js\",\"SCRIPT\"),(\"status.js\",\"STATUS_SCRIPT\"),(\"upload.js\",\"UPLOAD_SCRIPT\")]]" \
	  && node --check app.js && node tests/test_app_js.mjs app.js \
	  && node --check status.js && node tests/test_status_js.mjs status.js \
	  && node --check upload.js && node tests/test_upload_js.mjs upload.js \
	  && node --check src/video_to_website/assets/queue.js && node tests/test_queue_js.mjs src/video_to_website/assets/queue.js \
	  && node --check src/video_to_website/assets/progress.js && node tests/test_work_progress_js.mjs src/video_to_website/assets/progress.js \
	  && node --check src/video_to_website/assets/library.js && node tests/test_library_js.mjs src/video_to_website/assets/library.js \
	  && node --check src/video_to_website/assets/course.js && node tests/test_course_js.mjs src/video_to_website/assets/course.js \
	  && node --check src/video_to_website/assets/reader-state.js && node --check src/video_to_website/assets/reading.js && node tests/test_reading_js.mjs \
	  && node --check src/video_to_website/assets/settings.js \
	  && node --check src/video_to_website/assets/navigation.js && node tests/test_navigation_js.mjs src/video_to_website/assets/navigation.js \
	  && node --check src/video_to_website/assets/storage.js && node tests/test_storage_js.mjs src/video_to_website/assets/storage.js \
	  && node --check src/video_to_website/assets/workers.js && node tests/test_workers_js.mjs src/video_to_website/assets/workers.js \
	  && rm -f app.js status.js upload.js'

check:
	nix flake check

build:
	$(V2W) build $(COURSE) -o $(OUT) --llm $(LLM)

rebuild:
	$(V2W) build $(COURSE) -o $(OUT) --llm $(LLM) --force all

serve:
	$(V2W) serve $(OUT) -p $(PORT)

doctor:
	$(V2W) doctor

fmt:
	nix fmt

# x86_64-linux, so this needs a Linux machine or a remote builder. Proxmox has
# no NixOS template, so build this on the Proxmox host itself the first time.
lxc-image:
	nix build .$(HASH)packages.$(SYSTEM).lxc-image

# Builds on the server as well as deploying to it, so this works from a Mac:
# only the evaluation happens here.
deploy:
	$(NIXOS_REBUILD) switch --flake .$(HASH)lessons \
	  --target-host root@$(HOST) --build-host root@$(HOST)

deploy-dry:
	$(NIXOS_REBUILD) dry-activate --flake .$(HASH)lessons \
	  --target-host root@$(HOST) --build-host root@$(HOST)

clean-site:
	rm -rf $(OUT)

clean: clean-site
	rm -rf result result-* app.js
