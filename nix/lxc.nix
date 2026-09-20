# The container that gets imported into Proxmox. Everything here is about the
# box rather than the tool, so the module above stays reusable.
{ lib, ... }:
{
  services.video-to-website = {
    enable = true;
    # Outside the Nix store, which is world readable. Create it on the
    # container before the first deploy: see the runbook in the README.
    environmentFile = "/var/lib/secrets/v2w.env";
  };

  # The proxmox-lxc module defaults manageNetwork and manageHostName to false,
  # which hands both to Proxmox: it forces useNetworkd on and waits for Proxmox
  # to drop a .network file in, and forces hostName to "". But the container is
  # created with --ostype unmanaged precisely so Proxmox does not write into
  # /etc, so nothing configures the interface and it comes up with no address.
  # Own both here instead -- it is the reproducible half anyway.
  proxmoxLXC.manageNetwork = true;
  proxmoxLXC.manageHostName = true;

  networking.hostName = "lessons";

  # Static, so the bookmark keeps working. Check these against your LAN.
  networking.useDHCP = false;
  networking.interfaces.eth0.ipv4.addresses = [
    {
      address = "192.168.1.202";
      prefixLength = 24;
    }
  ];
  networking.defaultGateway = "192.168.1.1";
  networking.nameservers = [ "192.168.1.1" ];
  # For DHCP instead, drop the four settings above and set useDHCP = true.

  # nixos-rebuild --target-host needs to get in, so this key has to be in the
  # image you build -- an image built before it was added here accepts no key
  # at all, and with no root password there is then no way in over the
  # network. sshd also reads /root/.ssh/authorized_keys, which nothing here
  # manages, so a key put there by hand is the way to recover such a box.
  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "prohibit-password";
  };

  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFLS0gSRRtZrws7yfeQ3F9A9Wrchde+gQomtqysVQqL3"
  ];

  # There is no password on this box, so the Proxmox console cannot log in.
  # `pct enter <ctid>` from the host drops straight to a root shell without
  # one, which is the way in if SSH ever breaks. To have a console password
  # too, set one here -- but it lands world readable in the Nix store, so
  # change it with passwd once you are in:
  #   users.users.root.initialPassword = "changeme";

  # Nothing reads manpages in a container, and they are a large fraction of it.
  documentation.enable = lib.mkDefault false;

  time.timeZone = lib.mkDefault "UTC";
  system.stateVersion = "25.11";
}
