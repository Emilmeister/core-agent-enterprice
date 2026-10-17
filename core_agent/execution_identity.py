"""Private execution-host facts. Unknown hosts never imply stopped processes."""
import os
import re
import sys
from pathlib import Path


def execution_host():
    # PID 1 owns this dedicated container PID namespace. A worker-process restart
    # under a supervisor is deliberately not treated as a container restart.
    if sys.platform != "linux" or os.getpid() != 1:
        return None
    try:
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        identities = {}
        for line in mounts:
            fields = line.split()
            if len(fields) > 5 and fields[4] in {"/etc/hostname", "/etc/hosts"}:
                match = re.search(r"(?:^|/)docker/containers/([a-f0-9]{64})/(?:hostname|hosts)$", fields[3])
                if match:
                    identities[fields[4]] = match[1]
        if len(identities) == 2 and len(set(identities.values())) == 1:
            platform, slot = "docker", identities["/etc/hosts"]
        else:
            # Deployment-owned downward API, never request/model metadata.
            pod = os.environ.get("CORE_AGENT_POD_UID", "")
            container = os.environ.get("CORE_AGENT_CONTAINER_NAME", "")
            if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", pod):
                return None
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", container):
                return None
            platform, slot = "kubernetes", f"{pod}/{container}"
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.stat("/proc/1/ns/pid").st_ino
        start = int(Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19])
        host = dict(version=1, platform=platform, slot=slot, boot_id=boot,
                    pid_namespace=namespace, init_start=start)
        return host if _valid(host) else None
    except (OSError, ValueError, IndexError):
        return None


def _valid(host):
    return (isinstance(host, dict) and host.get("version") == 1
            and host.get("platform") in {"docker", "kubernetes"}
            and isinstance(host.get("slot"), str) and 0 < len(host["slot"]) <= 128
            and isinstance(host.get("boot_id"), str)
            and re.fullmatch(r"[a-f0-9-]{32,36}", host["boot_id"]) is not None
            and all(type(host.get(key)) is int and host[key] > 0 for key in ("pid_namespace", "init_start")))


def stopped_by_restart(previous, current):
    """Same runtime slot + a later PID-1 lifetime, not merely another namespace."""
    if not _valid(previous) or not _valid(current):
        return False
    if (previous["platform"], previous["slot"]) != (current["platform"], current["slot"]):
        return False
    # A changed boot can also be a different physical host. Without fencing
    # that host we cannot infer that an isolated predecessor has stopped.
    return (previous["boot_id"] == current["boot_id"]
            and current["init_start"] > previous["init_start"])
