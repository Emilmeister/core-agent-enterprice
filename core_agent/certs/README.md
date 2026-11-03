# Common MCP trust

MCP TLS uses the installed certifi roots plus `russian-trusted-root-ca.pem`.
The same trust roots apply to the destination and an HTTPS proxy; client identity
is loaded only into the destination context. Environment CA/proxy overrides do
not change this trust. To update corporate trust, review and update the pinned download
below and rebuild the image; owners cannot disable certificate verification.

The certificate is downloaded at build time from the official distribution URL:
`https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`.
The source repository does not contain the certificate. Docker runs
`sh deploy/fetch-mcp-ca.sh` before installing the project. For source runs,
tests, and wheel/sdist builds, run that command from the repository first
(requires curl and OpenSSL); CI does this after checkout.

The download requires HTTPS and a pinned file SHA-256:
`936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504`.
Download or checksum failure stops the build without replacing an existing file.
The generated certificate is ignored by Git and the Docker build context, but
included in Python distributions and the image. Runtime never downloads it.
An update requires reviewing the new certificate and changing the pinned hash.
The file checksum differs from the DER certificate fingerprint below.

Certificate identity (unchanged from the local Sber integration example):

- Subject/issuer: Russian Trusted Root CA, The Ministry of Digital Development
  and Communications, RU.
- Expires: 2032-02-27 21:04:15 UTC.
- SHA256: `d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31`.

Only this public CA is packaged. Client certificates, private keys, passwords and
the integration's `.env` are not part of the package.
