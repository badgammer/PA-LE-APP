"""
Shared helper for turning a domain name into a filesystem-safe certbot
--cert-name / lineage directory name.

certbot allows "*" in a --cert-name, but it's simpler and more portable
to avoid wildcard characters in directory names entirely. Both
bin/acme-renew.sh (which passes --cert-name to certbot) and webui/app.py
(which needs to find the resulting /etc/letsencrypt/live/<name>/ directory
to check expiry or offer a certificate download) import this so the
naming rule only has to be defined once and can never drift out of sync.
"""


def safe_cert_name(domain_name: str) -> str:
    """
    "*.example.com"  -> "wildcard.example.com"
    "vpn.example.com" -> "vpn.example.com"  (unchanged)
    """
    if domain_name.startswith("*."):
        return "wildcard." + domain_name[2:]
    return domain_name


if __name__ == "__main__":
    # Allows `python3 cert_naming.py '<name>'` from the shell (used by
    # bin/acme-renew.sh) without needing a separate -c one-liner.
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("Usage: cert_naming.py <domain-name>")
    print(safe_cert_name(sys.argv[1]))
