# Updating the appliance code

This page explains how to move a running appliance to the latest version of the code. It applies to both install profiles (single-instance and msp-panos). You do not need to say which one you have: the installer remembers it.

An update replaces the application code and systemd units. It does not touch your configuration, certificates, admin accounts or TLS certificates.

## The short version

Run these on the appliance. Everything except the two `sudo` lines runs as your normal user.

```bash
# One time only: get a permanent clone (skip if you already have one)
sudo dnf install -y git
git clone https://github.com/badgammer/PA-LE-APP "$HOME/PA-LE-APP"
```

```bash
# Every update
cd "$HOME/PA-LE-APP"

git fetch origin
git rev-parse --short HEAD                 # write this down: it is your rollback point
git log --oneline HEAD..origin/master      # what is new
git pull --ff-only origin master

ls /run/acme-appliance/*.lock 2>/dev/null  # should print nothing (no renewal running)

sudo bash ./iso-build/bootstrap-appliance.sh

sudo systemctl restart acme-webui.service          # single-instance profile
sudo systemctl restart acme-msp-console.service    # msp-panos profile
```

Then confirm it came up (see [Verify the update](#verify-the-update)).

> The clone URL above must match your repository. Use the same URL you originally cloned from (`git -C <your clone> remote -v` shows it).

## Why not `git pull` inside `/opt/acme-appliance`?

`/opt/acme-appliance` is a copy of the code, not a git checkout. `bootstrap-appliance.sh` copies the source there but deliberately leaves out `.git`, so `git pull` in that directory fails with `fatal: not a git repository`.

The update flow is therefore: pull into a separate clone, then run the installer, which copies the new code across. Some older notes in the README suggest `git pull` in `/opt/acme-appliance`; use this page instead.

## Before you start

**Get a clone that lasts.** The first-time instructions clone into `/tmp/acme-appliance-src`, which the OS cleans up automatically. If that directory is gone, clone again into your home directory as shown above.

**Pick a quiet time.** Restarting the web service stops any renewal that was started from its buttons. The scheduled renewals run as separate units and are not affected, but avoid their start times:

| Profile | Scheduled renewal |
|---|---|
| single-instance | `acme-renew.timer`, 03:30 plus up to 30 minutes |
| msp-panos | `acme-msp-renewal.timer`, midnight plus up to 30 minutes (every customer, one after another) |

On an msp-panos host one restart updates every customer at once, because there is only one process.

**Back up the configuration** (optional but cheap). This includes private keys, so keep the file private:

```bash
( umask 077; sudo tar czf "$HOME/acme-appliance-etc-$(date +%F).tar.gz" /etc/acme-appliance )
```

The appliance also keeps its own copy of each `appliance.yaml` every time you save in the UI (the last 30), in `/etc/acme-appliance/backups/` on a single-instance host and in `/etc/acme-appliance/customers/<slug>/backups/` for each msp-panos customer.

**Read what changed.** `CHANGES.md` has the newest entries at the top. Look for anything described as needing a manual step.

```bash
git show origin/master:CHANGES.md | head -80
```

**Use `master`, not a tag.** The `Current_Release` tag does not move with `master` and, at the time of writing, points at an older commit. Update from `master` unless you deliberately want a tagged release.

## What the installer does

`bootstrap-appliance.sh` is safe to run again. On an existing host it:

1. Makes sure the required OS packages are installed (this needs internet access).
2. Copies the source tree over `/opt/acme-appliance`, overwriting files with the same name. It skips `.git`, the `venv` directory and the image-building files.
3. Hands over to `install.sh`, which reuses the host's existing profile (read from `/etc/acme-appliance/profile`), installs any new Python requirements into the virtual environment, reinstalls the systemd units and runs `daemon-reload`.
4. Installs the daily certificate-expiry check timer (`acme-notify-check.timer`) if your version includes email alerting. On older hosts this is how the timer gets created, so you do not need to install it separately.

### What is preserved and what is replaced

| Preserved | Replaced |
|---|---|
| `appliance.yaml` (every customer's, on msp-panos) | Code under `/opt/acme-appliance` |
| Admin accounts (`users.yaml`, MSP `admins.yaml`) and session secret | systemd unit files |
| Web UI and MSP Console TLS certificates | The System Updates sudoers rule (single-instance) |
| Let's Encrypt accounts and issued certificates | |
| Notification settings and the python virtual environment | |

The installer only seeds `appliance.yaml` and the TLS certificates when they are missing, so it never overwrites yours.

Files that were deleted upstream stay on disk after an update because the copy does not delete anything. They are not used, so this is harmless.

## Why the restart is separate

The installer runs `systemctl enable --now`, which starts a unit that is stopped but does not restart one that is already running. The web service therefore keeps running the old code until you restart it:

```bash
sudo systemctl restart acme-webui.service          # single-instance
sudo systemctl restart acme-msp-console.service    # msp-panos
```

Everything else picks up the new code by itself: the renewal timers, the expiry check and the certbot hooks start a fresh process each time they run.

### If a renewal was running when you restarted

Restarting the service also ends renewals that were started from the UI buttons, and the lock file that makes the page show "Renewing..." can be left behind. If a domain keeps showing as renewing after a restart, check that nothing is actually running and clear the lock:

```bash
systemctl is-active acme-renew.service acme-msp-renewal.service   # both should say inactive
ls /run/acme-appliance/*.lock
sudo rm -f /run/acme-appliance/renew-*.lock /run/acme-appliance/redeploy-*.lock
```

Then start the renewal again from the UI.

## Verify the update

```bash
# Service is running (use the one for your profile)
systemctl status acme-webui.service --no-pager
systemctl status acme-msp-console.service --no-pager

# No errors on startup
journalctl -u acme-webui.service -n 50 --no-pager
journalctl -u acme-msp-console.service -n 50 --no-pager

# The UI answers (single-instance: 8443, msp-panos: 9443); expect 200
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443/login
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:9443/login

# Timers are scheduled
systemctl list-timers 'acme-*' --no-pager
```

Then log in and check the Domains page, run **Test connection** on one DNS provider and one deploy target, and open the Notifications page if your version has one.

Optionally record what is installed, so you can tell later which version a host is running:

```bash
git rev-parse HEAD | sudo tee /etc/acme-appliance/installed-commit
```

Application logs are in `/var/log/acme-appliance.log` (single-instance) and `/var/log/acme-appliance/msp-console.log` plus one log per customer under `/var/log/acme-appliance/customers/` (msp-panos).

## Updating from a zip (no GitHub access)

Use this when the host cannot reach GitHub, or when you are given the code as a zip.

```bash
sudo dnf install -y unzip
unzip PA-LE-APP.zip -d "$HOME"          # creates $HOME/PA-LE-APP
cd "$HOME/PA-LE-APP"
```

**Convert line endings if the zip was made on Windows.** A Windows working copy stores scripts with CRLF line endings, and bash cannot run them (typical errors are `$'\r': command not found` and `set: pipefail: invalid option name`). A `git clone` on Linux does not have this problem; a zip does. Convert the files the installer runs:

```bash
find . -type f \( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.timer' -o -name '*.cfg' \) \
     -exec sed -i 's/\r$//' {} +
find iso-build/sudoers.d -type f -exec sed -i 's/\r$//' {} +
```

If the zip contains only changed files rather than the whole repository, unzip it over your existing source tree first, then continue.

Then install and restart exactly as in the short version:

```bash
sudo bash ./iso-build/bootstrap-appliance.sh
sudo systemctl restart acme-webui.service          # or acme-msp-console.service
```

## Rolling back

Rolling back means installing the previous code again, using the commit you wrote down at the start:

```bash
cd "$HOME/PA-LE-APP"
git checkout <previous-commit>
sudo bash ./iso-build/bootstrap-appliance.sh
sudo systemctl restart acme-webui.service          # or acme-msp-console.service
```

When you want to go forward again, run `git checkout master && git pull --ff-only origin master` and repeat the install.

Configuration is forward compatible. Settings added by a newer version (for example the `notifications:` block) stay in `appliance.yaml` and are ignored by older code, so a rollback does not lose them. If you need to restore an earlier `appliance.yaml`, copy one from the `backups` folder (see above) over the live file and restart the service.

## Things to know

- **Do not change profile.** Run the installer with no flag. Passing `--msp` on a single-instance host, or the reverse, is refused. Changing profile means rebuilding the host.
- **Older msp-panos hosts need a rebuild.** Hosts built from the earlier design, with one `acmecust-<slug>` account and one `acme-webui@<slug>` unit per customer, are not migrated by the current installer. This page does not cover them.
- **Local edits in the clone.** If `git pull` complains about local changes, run `git status`; if the changes are not wanted, `git stash` sets them aside.
- **OS updates are separate.** This page is about the appliance's own code. Operating-system packages are handled by the System page (single-instance) or `dnf update` (any host).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `fatal: not a git repository` in `/opt/acme-appliance` | Expected, see [Why not git pull inside /opt/acme-appliance](#why-not-git-pull-inside-optacme-appliance). Run git in your clone instead. |
| `$'\r': command not found` or `set: pipefail: invalid option name` | The files have Windows line endings. Run the `sed` commands in the zip section, then repeat the install. |
| `does not look like an acme-appliance source tree (webui/app.py not found)` | You ran the script from the wrong place, or the zip is incomplete. Run it from the repository root, or pass the source path as the first argument. |
| `this host was already installed with profile '...'` | You passed a profile flag that does not match this host. Run with no flag. |
| The page still looks old, or new pages return 404 | The web service was not restarted. Restart it. |
| A domain shows "Renewing..." for a long time after the restart | A stale lock file. See [If a renewal was running when you restarted](#if-a-renewal-was-running-when-you-restarted). |
| `ModuleNotFoundError` in the service log | A new Python requirement was not installed, usually because the host had no internet access during the install. Fix connectivity and run the installer again. |
