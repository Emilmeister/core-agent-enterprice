# Common MCP trust

MCP TLS uses the installed certifi roots plus `russian-trusted-root-ca.pem`.
The same trust roots apply to the destination and an HTTPS proxy; client identity
is loaded only into the destination context. Environment CA/proxy overrides do
not change this trust. To update corporate trust, replace the public CA in the
package and rebuild the image; owners cannot disable certificate verification.

The public certificate was supplied in the local Sber integration example:

- Subject/issuer: Russian Trusted Root CA, The Ministry of Digital Development
  and Communications, RU.
- Expires: 2032-02-27 21:04:15 UTC.
- SHA256: `d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31`.

Only this public CA is packaged. Client certificates, private keys, passwords and
the integration's `.env` are not part of the package.
