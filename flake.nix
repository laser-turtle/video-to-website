{
  description = "video-to-website: turn tutorial videos into illustrated, timestamp-linked step-by-step sites";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      # Whisper weights, pinned by content hash so a rebuild fetches the same bytes.
      # To add a model: nix store prefetch-file \
      #   https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<name>.bin
      modelHashes = {
        "base.en" = "sha256-oDd5yG3zMjB19eeWyyzlAp8A7Ihp7uP9+4l6/jbG0AI=";
        "small.en" = "sha256-xhONbVjsyDIgl+D5h8MvG+i7ChhTKj+I9zTRu/nEHl0=";
      };

      mkPackages = pkgs:
        let
          inherit (pkgs) lib;
          python = pkgs.python3;

          runtimeDeps = [ pkgs.ffmpeg pkgs.whisper-cpp ];

          modelFile = name:
            pkgs.fetchurl {
              url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${name}.bin";
              hash = modelHashes.${name};
              name = "ggml-${name}.bin";
            };

          # A directory of models, which is what V2W_MODEL_PATH expects.
          modelDir = names:
            pkgs.runCommand "whisper-models" { } ''
              mkdir -p $out
              ${lib.concatMapStringsSep "\n"
                (name: "ln -s ${modelFile name} $out/ggml-${name}.bin")
                names}
            '';

          v2w = python.pkgs.buildPythonApplication {
            pname = "video-to-website";
            version = "0.1.0";
            pyproject = true;
            src = ./.;

            build-system = [ python.pkgs.hatchling ];
            dependencies = [ python.pkgs.anthropic ];

            # ffmpeg and whisper-cli must be on PATH wherever v2w runs.
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              "${lib.makeBinPath runtimeDeps}"
            ];

            doCheck = false;

            meta = {
              description = "Turn tutorial videos into illustrated, timestamp-linked step-by-step sites";
              mainProgram = "v2w";
              platforms = systems;
            };
          };

          # Same tool, with a whisper model baked in so nothing downloads at runtime.
          withModel = name:
            pkgs.symlinkJoin {
              name = "v2w-with-${lib.replaceStrings [ "." ] [ "-" ] name}";
              paths = [ v2w ];
              nativeBuildInputs = [ pkgs.makeWrapper ];
              postBuild = ''
                wrapProgram $out/bin/v2w \
                  --set-default V2W_MODEL_PATH ${modelDir [ name ]} \
                  --set-default V2W_MODEL ${name}
              '';
            };
        in
        {
          inherit v2w modelDir withModel runtimeDeps python;
        };
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          built = mkPackages pkgs;
        in
        {
          default = built.v2w;
          v2w = built.v2w;

          # Pinned whisper weights, as directories to point V2W_MODEL_PATH at.
          whisper-models-base-en = built.modelDir [ "base.en" ];
          whisper-models-small-en = built.modelDir [ "small.en" ];
          whisper-models = built.modelDir [ "base.en" "small.en" ];

          # Self-contained: no model download on first run.
          v2w-with-small-en = built.withModel "small.en";
          v2w-with-base-en = built.withModel "base.en";
        }
        // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          # The LXC tarball to upload to Proxmox.
          lxc-image = self.nixosConfigurations.lessons.config.system.build.tarball;
        }
        // nixpkgs.lib.optionalAttrs (nixpkgs.lib.hasSuffix "linux" system) {
          # GPU transcription for a Linux box with an NVIDIA card.
          # Unfree and built from source, so it takes a while the first time.
          v2w-cuda =
            let
              pkgsCuda = import nixpkgs {
                inherit system;
                config = {
                  allowUnfree = true;
                  cudaSupport = true;
                };
              };
            in
            (mkPackages pkgsCuda).v2w;
        });

      nixosModules.default = import ./nix/module.nix { inherit self; };
      nixosModules.video-to-website = self.nixosModules.default;

      # Importable straight into Proxmox; after that, deploy with
      # nixos-rebuild switch --flake .#lessons --target-host root@<ip>
      nixosConfigurations.lessons = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          "${nixpkgs}/nixos/modules/virtualisation/proxmox-lxc.nix"
          self.nixosModules.default
          ./nix/lxc.nix
        ];
      };

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/v2w";
          meta.description = "Turn tutorial videos into a step-by-step website";
        };
      });

      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          built = mkPackages pkgs;
          pythonEnv = built.python.withPackages (ps: [ ps.anthropic ]);
        in
        {
          default = pkgs.mkShell {
            # nodejs is here for tests/test_app_js.mjs, which checks the page
            # script against a DOM stub.
            packages = built.runtimeDeps ++ [ pythonEnv pkgs.ruff pkgs.nodejs pkgs.gnumake ];
            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export V2W_MODEL_DIR="''${V2W_MODEL_DIR:-$PWD/.v2w-models}"
              v2w() { python -m video_to_website.cli "$@"; }
              echo "video-to-website dev shell"
              echo "  v2w doctor                 check tools and credentials"
              echo "  v2w fetch-model small.en   download a whisper model"
              echo "  v2w build /path/to/course  build the site"
              echo "  make                       list the shortcuts"
            '';
          };
        });

      checks = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          built = mkPackages pkgs;
        in
        {
          package = self.packages.${system}.default;

          tests = pkgs.runCommand "video-to-website-tests"
            {
              nativeBuildInputs = [
                (built.python.withPackages (ps: [ ps.anthropic ]))
                pkgs.nodejs
              ];
            } ''
            cp -r ${./src} ./src
            cp -r ${./tests} ./tests
            export PYTHONPATH=$PWD/src
            python -m unittest discover -s tests -v

            # The page scripts only misbehave at runtime, so check them against DOM stubs.
            python -c "from video_to_website.render import SCRIPT; open('app.js','w').write(SCRIPT)"
            node --check app.js
            node tests/test_app_js.mjs app.js

            python -c "from video_to_website.render import STATUS_SCRIPT; open('status.js','w').write(STATUS_SCRIPT)"
            node --check status.js
            node tests/test_status_js.mjs status.js
            touch $out
          '';
        });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixpkgs-fmt);
    };
}
