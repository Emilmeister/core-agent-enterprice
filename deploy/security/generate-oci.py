"""Generate native OCI profiles from the pinned, capability-free Moby baseline."""

import hashlib
import json
from pathlib import Path


root = Path(__file__).resolve().parent
source = json.loads((root / "upstream/moby-default.json").read_text())
for arch, native in (("amd64", "SCMP_ARCH_X86_64"), ("arm64", "SCMP_ARCH_AARCH64")):
    rules = []
    for original in source["syscalls"]:
        includes, excludes = original.get("includes", {}), original.get("excludes", {})
        if includes.get("caps") or (includes.get("arches") and arch not in includes["arches"]):
            continue
        if arch in excludes.get("arches", ()):
            continue
        if includes.get("minKernel", "0") not in {"0", "4.8"}:
            raise ValueError("Review new upstream kernel predicate before regenerating")
        rules.append({key: value for key, value in original.items()
                      if key not in {"includes", "excludes", "comment"}})
    rules.append({"names": ["clone", "unshare", "setns", "mount", "umount2", "pivot_root"],
                  "action": "SCMP_ACT_ALLOW"})
    profile = {"defaultAction": source["defaultAction"],
               "defaultErrnoRet": source["defaultErrnoRet"],
               "architectures": [native], "syscalls": rules}
    (root / f"oci-{arch}.json").write_text(json.dumps(profile, indent=2) + "\n")

files = ["oci-amd64.json", "oci-arm64.json", "upstream/moby-default.json",
         "upstream/LICENSE-moby", "generate-oci.py", "PROVENANCE.md"]
(root / "SHA256SUMS").write_text("".join(
    f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}\n"
    for name in files
))
