#!/bin/bash
# This machine's network sits behind a proxy that re-signs HTTPS traffic with a
# self-signed certificate, which the venv's default CA bundle (certifi) doesn't
# trust. Without this, any HTTPS call (pip install, Google APIs, weather API, etc.)
# fails with SSL: CERTIFICATE_VERIFY_FAILED.
#
# This exports the trusted certs from the macOS keychains (which the OS/curl
# already trust), validates + dedupes each one, reinstalls a clean certifi package,
# then appends the validated certs to its bundle. Re-run after any venv rebuild.
#
# NOTE: earlier versions of this script blindly concatenated keychain exports onto
# an already-patched cacert.pem, which corrupted it (NO_CERTIFICATE_OR_CRL_FOUND)
# and caused confusing, inconsistent failures. This version always starts from a
# freshly reinstalled certifi and validates every cert before appending.
set -e
cd "$(dirname "$0")"

security find-certificate -a -p /Library/Keychains/System.keychain > .ca-bundle.pem 2>/dev/null
security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain >> .ca-bundle.pem 2>/dev/null
security find-certificate -a -p ~/Library/Keychains/login.keychain-db >> .ca-bundle.pem 2>/dev/null

python3 - <<'EOF'
import re, subprocess

with open(".ca-bundle.pem") as f:
    content = f.read()

certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", content, re.DOTALL)

valid, seen = [], set()
for cert in certs:
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
        input=cert, capture_output=True, text=True,
    )
    if result.returncode != 0:
        continue
    fp = result.stdout.strip()
    if fp in seen:
        continue
    seen.add(fp)
    valid.append(cert)

with open(".ca-bundle-clean.pem", "w") as f:
    f.write("\n".join(valid) + "\n")

print(f"{len(certs)} cert(s) found, {len(valid)} valid unique cert(s) kept")
EOF

# Always start from a clean, freshly reinstalled certifi so repeated runs of this
# script can't compound corruption from a previous run.
PIP_CERT="$(pwd)/.ca-bundle-clean.pem" ./venv/bin/pip install --quiet --force-reinstall --no-deps certifi

CERTIFI_PEM="$(./venv/bin/python -c 'import certifi; print(certifi.where())')"
{
  echo ""
  echo "# --- wardrobe-assistant proxy CA bundle (validated, see .ca-bundle-clean.pem) ---"
  cat .ca-bundle-clean.pem
} >> "$CERTIFI_PEM"

echo "Patched: $CERTIFI_PEM"
