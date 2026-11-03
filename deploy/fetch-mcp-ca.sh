#!/bin/sh
# Prepare the public MCP trust root before packaging or running from source.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
destination="$root/core_agent/certs/russian-trusted-root-ca.pem"
temporary=$(mktemp "$destination.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM

curl --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 60 --retry 2 \
    https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
    --output "$temporary"
checksum=$(openssl dgst -sha256 -r "$temporary")
if [ "${checksum%% *}" != "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504" ]; then
    echo 'MCP CA download failed SHA-256 verification' >&2
    exit 1
fi
chmod 644 "$temporary"
mv -f "$temporary" "$destination"
