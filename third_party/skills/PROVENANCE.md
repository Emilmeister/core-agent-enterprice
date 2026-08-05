# Vendored agent skills

These packages are adapted, self-contained derivatives of the upstream skills
listed below. The repository stores the reviewed files used by the image; Docker
builds do not download moving branches. `SHA256SUMS` pins the bytes copied into
the image.

These are not byte-for-byte mirrors of the upstream development directories.
The local packages contain only reviewed runtime guidance referenced by their
adapted `SKILL.md`; executable examples, creation logs, and upstream test assets
are intentionally omitted. The upstream commit/tree identifies origin and
license, while `SHA256SUMS` is the authoritative manifest of the shipped bytes.

| Local package | Upstream repository and commit | Upstream path | Tree | License |
|---|---|---|---|---|
| `systematic-debugging` | `obra/superpowers@44c9b2d6e889982ac18c27d05a19fefe335194e1` | `skills/systematic-debugging` | `ab83fc82f82582e047d96fc516bac9bc03095ee0` | MIT |
| `verification-before-completion` | `obra/superpowers@44c9b2d6e889982ac18c27d05a19fefe335194e1` | `skills/verification-before-completion` | `a4cb0b69aaefeab540947a7f1642bdaad810e37a` | MIT |
| `knowledge-synthesis` | `anthropics/knowledge-work-plugins@2099f2c2fcddf5a129f42da0e3788291a89662a3` | `enterprise-search/skills/knowledge-synthesis` | `c79597fffe8c754779af5802d5c1bce392821724` | Apache-2.0 |
| `explore-data` | `anthropics/knowledge-work-plugins@2099f2c2fcddf5a129f42da0e3788291a89662a3` | `data/skills/explore-data` | `bee4c8c0603859fd60c3622dd41e7a093476fcbf` | Apache-2.0 |
| `validate-data` | `anthropics/knowledge-work-plugins@2099f2c2fcddf5a129f42da0e3788291a89662a3` | `data/skills/validate-data` | `270c075639c4e5d1dac85478eac215d34dfe2268` | Apache-2.0 |
| `statistical-analysis` | `anthropics/knowledge-work-plugins@2099f2c2fcddf5a129f42da0e3788291a89662a3` | `data/skills/statistical-analysis` | `43c75ea64ed2726d5d3b713c3312dc4537444fe2` | Apache-2.0 |
| `sql-queries` | `anthropics/knowledge-work-plugins@2099f2c2fcddf5a129f42da0e3788291a89662a3` | `data/skills/sql-queries` | `508444de634ec4ba9178750b6cbc897fe9f9c169` | Apache-2.0 |

Adaptations remove assumptions about slash commands, coding-agent-only tools,
write access, and vendor-specific connector syntax. They add bounded evidence,
tenant/privacy constraints, truthful budget exhaustion, and strict read-only SQL
guidance for the Core Agent runtime. Upstream license texts are in `LICENSES/`.
