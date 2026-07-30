#!/usr/bin/env bash
set -euo pipefail

CERT_DIR="/etc/acme-appliance/webui-tls"
CERT="$CERT_DIR/webui.crt"
KEY="$CERT_DIR/webui.key"

mkdir -p "$CERT_DIR"

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "Certificate already exists at $CERT - not overwriting."
  exit 0
fi

CN="${1:-acme-appliance.local}"

openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout "$KEY" -out "$CERT" \
  -subj "/CN=${CN}" \
  -addext "subjectAltName=DNS:${CN}"

chmod 600 "$KEY"
chmod 644 "$CERT"

echo "Generated self-signed certificate for CN=${CN}:"
echo "  $CERT"
echo "  $KEY"
