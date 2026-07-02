#!/bin/bash
# This machine's network sits behind a proxy that re-signs HTTPS traffic with a
# self-signed certificate, which the venv's default CA bundle (certifi) doesn't
# trust. Without this, any HTTPS call (pip install, Google APIs, weather API, etc.)
# fails with SSL: CERTIFICATE_VERIFY_FAILED.
#
# This exports the trusted certs from the macOS keychains (which the OS/curl
# already trust) and appends them to the venv's certifi bundle, so every Python
# HTTP library in the venv trusts them too. Re-run this after any venv rebuild.
set -e
cd "$(dirname "$0")"

security find-certificate -a -p /Library/Keychains/System.keychain > .ca-bundle.pem 2>/dev/null
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain >> .ca-bundle.pem 2>/dev/null
security find-certificate -a -p ~/Library/Keychains/login.keychain-db >> .ca-bundle.pem 2>/dev/null

CERTIFI_PEM="$(./venv/bin/python -c 'import certifi; print(certifi.where())')"

if grep -q "wardrobe-assistant proxy CA bundle" "$CERTIFI_PEM" 2>/dev/null; then
  echo "Already patched: $CERTIFI_PEM"
else
  {
    echo ""
    echo "# --- wardrobe-assistant proxy CA bundle (see .ca-bundle.pem) ---"
    cat .ca-bundle.pem
  } >> "$CERTIFI_PEM"
  echo "Patched: $CERTIFI_PEM"
fi
