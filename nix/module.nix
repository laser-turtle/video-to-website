# A NixOS service that watches a library of course videos and keeps a built
# site beside it. Deployed to a box that is not the one it was written on, so
# everything it needs is either in the closure or in stateDir.
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.video-to-website;
  system = pkgs.stdenv.hostPlatform.system;
  inherit (lib) mkEnableOption mkIf mkOption optional optionals types;

  # Videos arrive here by scp as root as often as through the upload page, and
  # a root-owned course folder is one the service can read but not write: the
  # build works and the next upload into it fails. Runs as root before the
  # service drops privileges, so the library always belongs to it.
  takeLibrary = pkgs.writeShellApplication {
    name = "v2w-take-library";
    runtimeInputs = [ pkgs.coreutils pkgs.findutils ];
    text = ''
      library=${cfg.stateDir}/library
      install -d -m 0775 -o ${cfg.user} -g ${cfg.group} "$library" || exit 0
      # Each step keeps going on failure and the unit ignores the result: a
      # file this cannot chown should cost one upload, never the whole service.
      chown -R ${cfg.user}:${cfg.group} "$library" || true
      # Plain 0775, no setgid: RestrictSUIDSGID= is not among the settings a
      # '+' prefix lifts, so a chmod setting the sgid bit may be refused by the
      # seccomp filter. Ownership is what actually matters here anyway.
      find "$library" -type d -exec chmod 0775 {} + || true
    '';
  };
in
{
  options.services.video-to-website = {
    enable = mkEnableOption "the video-to-website lesson builder";

    package = mkOption {
      type = types.package;
      default = self.packages.${system}.default;
      defaultText = "the flake's v2w package";
      description = "Which build of v2w to run.";
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/video-to-website";
      description = ''
        Holds library/ (drop course folders here), site/ (generated) and
        work/ (stage cache, safe to delete).
      '';
    };

    user = mkOption { type = types.str; default = "v2w"; };
    group = mkOption { type = types.str; default = "v2w"; };

    interval = mkOption {
      type = types.ints.positive;
      default = 30;
      description = "Seconds between checks of the library.";
    };

    whisperModel = mkOption {
      type = types.str;
      default = "small.en";
      description = "Which whisper model to transcribe with.";
    };

    whisperModels = mkOption {
      type = types.nullOr types.package;
      default = self.packages.${system}.whisper-models-small-en;
      defaultText = "the flake's pinned small.en model";
      description = ''
        A directory of ggml models, pinned by hash, so the service never
        downloads weights at runtime. Set to null to let it fetch its own.
      '';
    };

    llm = mkOption {
      type = types.enum [ "auto" "anthropic" "claude-cli" "ollama" "codex-cli" "heuristic" ];
      default = "anthropic";
      description = ''
        The API rather than the CLI by default: a server running batches wants
        predictable per-token billing, not a session quota it can exhaust.
      '';
    };

    llmModel = mkOption {
      type = types.nullOr types.str;
      default = "claude-sonnet-5";
      description = "Model for the chosen backend. Null uses the backend's own default.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/var/lib/secrets/v2w.env";
      description = ''
        A file of KEY=value lines, read at start. Put ANTHROPIC_API_KEY here so
        it stays out of the world-readable Nix store.
      '';
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "--clip-crf" "26" ];
      description = "Further arguments for `v2w watch`.";
    };

    nginx = mkOption {
      type = types.bool;
      default = true;
      description = "Serve the built site over HTTP.";
    };

    uploads = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Offer the site's upload page, which PUTs videos straight into the
        library. There is no authentication in front of it, so anyone who can
        reach the site can add a course -- fine on a home network, not on one
        you share.
      '';
    };

    apiPort = mkOption {
      type = types.port;
      default = 8765;
      description = ''
        Loopback port the upload API listens on. nginx proxies /api/ to it;
        nothing off the box talks to it directly.
      '';
    };

    hostName = mkOption {
      type = types.str;
      default = "lessons";
      description = "Virtual host name to serve the site as.";
    };

    openFirewall = mkOption { type = types.bool; default = true; };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      description = "video-to-website builder";
    };
    users.groups.${cfg.group} = { };

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir}       0755 ${cfg.user} ${cfg.group} -"
      # Group-writable: this is where videos get dropped, often over a share.
      "d ${cfg.stateDir}/library 0775 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/site    0755 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/work    0750 ${cfg.user} ${cfg.group} -"
    ];

    systemd.services.video-to-website = {
      description = "Build lesson pages from course videos";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = lib.optionalAttrs (cfg.whisperModels != null) {
        V2W_MODEL_PATH = "${cfg.whisperModels}";
      };

      serviceConfig = {
        ExecStart = lib.escapeShellArgs ([
          "${cfg.package}/bin/v2w"
          "watch"
          "${cfg.stateDir}/library"
          "--out"
          "${cfg.stateDir}/site"
          "--work"
          "${cfg.stateDir}/work"
          "--interval"
          (toString cfg.interval)
          "--model"
          cfg.whisperModel
          "--llm"
          cfg.llm
        ]
        ++ optionals (cfg.llmModel != null) [ "--llm-model" cfg.llmModel ]
        ++ optionals cfg.uploads [ "--api-port" (toString cfg.apiPort) "--api-bind" "127.0.0.1" ]
        ++ cfg.extraArgs);

        # '+' runs it as root, before User= takes effect; '-' keeps a failure
        # here from stopping the service, which would turn a permissions
        # nuisance into an outage.
        ExecStartPre = "-+${takeLibrary}/bin/v2w-take-library";

        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;

        Restart = "on-failure";
        RestartSec = 10;
        # Transcoding for hours should not make the rest of the box unusable.
        Nice = 10;
        IOSchedulingClass = "idle";

        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
      };
    };

    services.nginx = mkIf cfg.nginx {
      enable = true;
      recommendedGzipSettings = true;
      recommendedOptimisation = true;
      virtualHosts.${cfg.hostName} = {
        default = true;
        root = "${cfg.stateDir}/site";
        # The site is rebuilt in place under unchanging names, so revalidate.
        # Stylesheet and script carry a content hash, and are safe to keep.
        locations."/".extraConfig = ''
          add_header Cache-Control "no-cache";
        '';
        locations."/assets/".extraConfig = ''
          add_header Cache-Control "public, max-age=31536000, immutable";
        '';
        locations."/api/" = mkIf cfg.uploads {
          proxyPass = "http://127.0.0.1:${toString cfg.apiPort}";
          extraConfig = ''
            # A lesson is a couple of gigabytes, so no cap on the body.
            client_max_body_size 0;
            # And stream it through rather than spooling the whole upload to
            # nginx's disk first: buffered, the browser's progress bar would
            # fill up long before the file had gone anywhere.
            proxy_request_buffering off;
            proxy_http_version 1.1;
            proxy_read_timeout 2h;
            proxy_send_timeout 2h;
          '';
        };
      };
    };

    # So nginx can read the site and follow its symlinks into the library.
    users.users.nginx.extraGroups = mkIf cfg.nginx [ cfg.group ];

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ 80 ];
  };
}
