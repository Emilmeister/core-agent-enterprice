"""Opt-in bounded operator diagnostics, without credentials or exception bodies."""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import time

from .errors import CoreError, SandboxDiagnostics


class StartupDiagnostics:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.logger = logging.getLogger("core_agent.runtime")

    def emit(self, event, *, failed=False, **fields):
        if self.enabled:
            self.logger.log(logging.CRITICAL if failed else logging.INFO,
                            json.dumps({"event": event, **fields}, sort_keys=True))

    @contextmanager
    def stage(self, name):
        started = time.monotonic()
        self.emit("startup.stage", stage=name, status="started")
        try:
            yield
        except Exception as error:
            details = {}
            if (isinstance(error, CoreError) and isinstance(error.code, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error.code)):
                details["error_code"] = error.code
            else:
                details["error_code"] = "STARTUP_FAILED"
            if isinstance(error, OSError) and type(error.errno) is int:
                details["errno"] = error.errno
            if type(getattr(error, "diagnostics", None)) is SandboxDiagnostics:
                details["sandbox"] = error.diagnostics.as_dict()
            self.emit("startup.stage", failed=True, stage=name, status="failed",
                      elapsed_ms=int((time.monotonic() - started) * 1000), **details)
            raise
        else:
            self.emit("startup.stage", stage=name, status="succeeded",
                      elapsed_ms=int((time.monotonic() - started) * 1000))

    @staticmethod
    def _read(path, pattern):
        try:
            with open(path, encoding="ascii") as source:
                value = source.read(128).strip()
            return value if re.fullmatch(pattern, value) else "unavailable"
        except (OSError, UnicodeError):
            return "unavailable"

    def platform(self):
        if not self.enabled:
            return
        system, architecture = platform.system(), platform.machine()
        helpers = {name: shutil.which(name, path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin") is not None
                   for name in ("bwrap", "slirp4netns", "nft", "nsenter", "ip")}
        cgroups = {name.replace(".", "_"): self._read("/sys/fs/cgroup/" + name, r"(?:max|[0-9]{1,20})(?: [0-9]{1,20})?")
                   for name in ("cpu.max", "memory.max", "memory.current", "pids.max", "pids.current")}
        namespaces = {name: self._read("/proc/sys/" + path, r"[0-9]{1,20}") for name, path in (
            ("unprivileged_userns_clone", "kernel/unprivileged_userns_clone"),
            ("max_user_namespaces", "user/max_user_namespaces"),
            ("apparmor_restrict_unprivileged_userns", "kernel/apparmor_restrict_unprivileged_userns"),
        )}
        process = {}
        try:
            with open("/proc/self/status", encoding="ascii") as source:
                for line in source.read(8192).splitlines():
                    key, _, value = line.partition(":")
                    if key in {"NoNewPrivs", "Seccomp", "CapEff"} and re.fullmatch(r"[0-9a-fA-F]{1,20}", value.strip()):
                        process[key] = value.strip()
        except (OSError, UnicodeError):
            pass
        try:
            tun = stat.S_ISCHR(Path("/dev/net/tun").stat().st_mode)
        except OSError:
            tun = False
        self.emit("startup.platform", system=system if system in {"Linux", "Darwin", "Windows"} else "other",
                  architecture=architecture if architecture in {"x86_64", "aarch64", "arm64", "AMD64"} else "other",
                  pidfd=hasattr(os, "pidfd_open"), tun_device=tun, helpers=helpers,
                  cgroups=cgroups, namespaces=namespaces, process=process)

    def sandbox(self):
        if not self.enabled or platform.system() != "Linux":
            return
        from .sandbox_diagnostics import run_probes
        run_probes(self)
