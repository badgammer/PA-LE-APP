# Building a prebuilt appliance image

Three options here, in order of least to most "hands-off." All three end
up running the exact same `bootstrap-appliance.sh`, which does the real
work (install packages, copy the appliance, create the venv, set
permissions, install/enable systemd units, generate a self-signed cert,
create certbot's own working directories).

> Producing an actual bootable `.iso`/`.qcow2` requires internet access
> (to download a Rocky Linux ISO) and tools (`xorriso`, `packer`,
> `qemu-img`) not available in this assistant's sandbox. The scripts here
> are complete and ready to run on your own build machine.

## A note on python3-venv

Unlike Debian/Ubuntu, Rocky/RHEL does **not** ship a separate
`python3-venv` package -- the `venv` module is already part of the base
`python3` package. If you add `python3-venv` to a `dnf install` command
on Rocky/RHEL, dnf will fail to resolve that package name and abort the
**entire** transaction, silently preventing everything else in that same
command (including `certbot`) from installing too. `bootstrap-appliance.sh`
and `ks.cfg` deliberately do NOT list `python3-venv` for this reason.

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

Output: `output-acme-appliance/acme-appliance.qcow2`. A commented-out
`vsphere-iso` source in `appliance.pkr.hcl` builds directly into vCenter
instead if that's your target.

## After first boot (any option)

1. Browse to `https://<appliance-ip>:8443/` and create the admin account.
2. Add DNS provider(s), firewall(s), and domain(s). For each Palo Alto
   firewall's admin role, make sure **Configuration**, **Import**,
   **Commit**, and **Operational Requests** are all enabled under
   Device > Admin Roles > &lt;role&gt; > XML API tab -- missing
   "Operational Requests" specifically causes the web UI's
   "Test connection" button to fail even though real deployments work.
3. Click **Run renewal now** (or a domain's **Renew now**) for a first
   test issuance -- point `acme.server` at the Let's Encrypt *staging*
   endpoint first to avoid rate limits while testing.
