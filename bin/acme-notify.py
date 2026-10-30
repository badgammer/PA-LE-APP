#!/usr/bin/env python3
"""
Email alerting CLI for the ACME appliance. Thin wrapper around notifications.py.

  acme-notify.py test                                   send a test email with the saved settings
  acme-notify.py event renewal_failed --domain D --detail "..."
                                                        send an alert (always exits 0: an alert problem
                                                        must never fail a renewal/deploy script)
  acme-notify.py check-expiry [--all-customers] [--dry-run]
                                                        scan certificates, email one digest if any are
                                                        close to expiry (run daily by the timer installed
                                                        with bin/install-notify-timer.sh)

Settings come from the appliance's own appliance.yaml (ACME_APPLIANCE_CONFIG, default
/etc/acme-appliance/appliance.yaml). Under msp-panos use --config /etc/acme-appliance/customers/<slug>/appliance.yaml
or --all-customers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notifications import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
