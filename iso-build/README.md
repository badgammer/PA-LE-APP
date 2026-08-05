# Building a prebuilt appliance image

Three options here, in order of least to most "hands-off." All three end
up running the exact same `bootstrap-appliance.sh`, which also installs
the System Updates feature's systemd units and sudoers rule.

> Producing an actual bootable `.iso`/`.qcow2` requires internet access
> and tools (`xorriso`, `packer`, `qemu-img`) not available in this
> assistant's sandbox. The scripts here are complete and ready to run on
> your own build machine.

## Option 1: Bootstrap script only (fastest, no ISO tooling at all)

```bash
git clone <your-repo-url> /tmp/acme-appliance-src
cd /tmp/acme-appliance-src
sudo ./iso-build/bootstrap-appliance.sh
```

## Option 2: Unattended install ISO

```bash
sudo dnf install -y xorriso syslinux genisoimage
sudo ./iso-build/build-iso.sh /path/to/Rocky-9-x86_64-minimal.iso acme-appliance.iso
```

## Option 3: Packer image build

```bash
cd iso-build
packer init appliance.pkr.hcl
packer build \
  -var 'iso_url=/path/to/Rocky-9-x86_64-minimal.iso' \
  -var 'iso_checksum=sha256:<checksum>' \
  appliance.pkr.hcl
```

## What bootstrap-appliance.sh sets up for System Updates

- Installs `dnf-utils` (provides `needs-restarting`, used for
  reboot-required detection).
- Copies and enables `acme-appliance-check-updates.service`,
  `acme-appliance-updates.service`, and `acme-appliance-reboot.service`
  (NOT enabled at boot -- only started on demand from the web UI).
- Installs and validates (`visudo -c`) the sudoers rule at
  `/etc/sudoers.d/acme-appliance-updates` that lets the unprivileged
  `acme-appliance` account start exactly those 3 units -- nothing else.
- Installs `python-pam` into the venv for the step-up authentication
  check (a real Linux sudo-group account's password, verified via PAM,
  required before Apply Updates or Reboot will run).

See the main `README.md`'s "System Updates feature" section for the full
security model.

## After first boot (any option)

1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Add DNS provider(s), firewall(s), and domain(s). Make sure each Palo
   Alto firewall's admin role has Configuration, Import, Commit, and
   Operational Requests enabled.
3. Visit the **System** page to confirm "Check for updates" works.
   "Apply updates"/"Reboot" will ask for a Linux username/password from
   the appliance's `wheel` group -- this is separate from the web UI login.
