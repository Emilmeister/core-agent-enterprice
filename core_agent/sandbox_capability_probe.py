"""Fixed, stdlib-only capability checks in disposable processes, never user code."""
import ctypes
import errno
import json
import os
import platform
import sys


_OPERATIONS = {
    "platform", "no_new_privileges", "landlock_abi", "landlock_baseline",
    "landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self",
    "landlock_verify", "seccomp_library", "seccomp_baseline", "seccomp_init",
    "seccomp_rule", "seccomp_load", "seccomp_verify",
}
_REASONS = {"completed", "permission_denied", "unsupported", "disabled",
            "invalid_argument", "resource_limit", "os_error", "dependency_missing",
            "verification_failed", "internal_error"}


def parse_result(raw, returncode):
    """Allow only bounded, typed diagnostic fields from our own fixed helper."""
    invalid = {"status": "failed", "reason": "invalid_probe_result"}
    try:
        row = json.loads(raw)
        if (not isinstance(row, dict) or not {"status", "reason", "operation"} <= row.keys()
                or row.keys() - {"status", "reason", "operation", "errno", "abi"}
                or row["status"] not in {"succeeded", "failed"}
                or row["reason"] not in _REASONS or row["operation"] not in _OPERATIONS
                or returncode not in {0, 1}
                or (row["status"] == "succeeded") != (returncode == 0)
                or (row["reason"] == "completed") != (returncode == 0)):
            return invalid
        for name, ceiling in (("errno", 4095), ("abi", 255)):
            if name in row and (type(row[name]) is not int or not 1 <= row[name] <= ceiling):
                return invalid
        if returncode == 0 and "errno" in row:
            return invalid
        return row
    except (ValueError, TypeError):
        return invalid


def _checked(result, operation, value, *, negative_errno=False):
    result["operation"] = operation
    if value < 0:
        raise OSError(-value if negative_errno else ctypes.get_errno(), "")
    return value


def _no_new_privileges(libc, result):
    _checked(result, "no_new_privileges", libc.prctl(38, 1, 0, 0, 0))


def _landlock(libc, result, *, filesystem):
    # Linux x86_64/aarch64 UAPI: create_ruleset=444, add_rule=445, restrict_self=446.
    result["abi"] = _checked(result, "landlock_abi", libc.syscall(
        444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1)))
    if not filesystem:
        return
    result["operation"] = "landlock_baseline"
    for path in ("/dev/null", "/dev/zero"):
        os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
    read_file = 1 << 2
    attributes = ctypes.c_uint64(read_file)
    ruleset = _checked(result, "landlock_create_ruleset", libc.syscall(
        444, ctypes.byref(attributes), ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0)))
    try:
        class PathBeneath(ctypes.Structure):
            _pack_ = 1
            _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

        path_fd = os.open("/dev/null", os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathBeneath(read_file, path_fd)
            _checked(result, "landlock_add_rule", libc.syscall(
                445, ctypes.c_int(ruleset), ctypes.c_uint(1), ctypes.byref(rule), ctypes.c_uint(0)))
        finally:
            os.close(path_fd)
        _no_new_privileges(libc, result)
        _checked(result, "landlock_restrict_self", libc.syscall(446, ctypes.c_int(ruleset), ctypes.c_uint(0)))
    finally:
        os.close(ruleset)
    result["operation"] = "landlock_verify"
    os.close(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
    try:
        descriptor = os.open("/dev/zero", os.O_RDONLY | os.O_CLOEXEC)
    except OSError as error:
        if error.errno != errno.EACCES:
            raise
    else:
        os.close(descriptor)
        raise ValueError("restriction did not deny open")


def _seccomp(libc, result):
    result["operation"] = "seccomp_library"
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    number = _checked(result, "seccomp_baseline", library.seccomp_syscall_resolve_name(b"getppid"),
                      negative_errno=True)
    _checked(result, "seccomp_baseline", libc.syscall(number))  # PID 1 can have parent PID 0.
    result["operation"] = "seccomp_init"
    context = library.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError(errno.ENOMEM, "")
    try:
        _checked(result, "seccomp_rule", library.seccomp_rule_add_array(
            context, 0x00050000 | errno.EPERM, number, 0, None), negative_errno=True)
        _no_new_privileges(libc, result)
        _checked(result, "seccomp_load", library.seccomp_load(context), negative_errno=True)
        result["operation"] = "seccomp_verify"
        ctypes.set_errno(0)
        if libc.syscall(number) != -1 or ctypes.get_errno() != errno.EPERM:
            raise ValueError("restriction did not deny syscall")
    finally:
        library.seccomp_release(context)


def main():
    result = {"status": "failed", "reason": "unsupported", "operation": "platform"}
    try:
        if (sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}
                or ctypes.sizeof(ctypes.c_void_p) != 8):
            raise OSError(errno.ENOSYS, "")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.argtypes = [ctypes.c_long]
        libc.syscall.restype = ctypes.c_long
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        if sys.argv[1:] == ["seccomp_filter"]:
            _seccomp(libc, result)
        elif sys.argv[1:] in (["landlock_abi"], ["landlock_filesystem"]):
            _landlock(libc, result, filesystem=sys.argv[1] == "landlock_filesystem")
        else:
            raise ValueError("unknown fixed probe")
        result.update(status="succeeded", reason="completed")
    except OSError as error:
        result["reason"] = {
            errno.EPERM: "permission_denied", errno.EACCES: "permission_denied",
            errno.ENOSYS: "unsupported", errno.EOPNOTSUPP: "disabled",
            errno.EINVAL: "invalid_argument", errno.ENOMEM: "resource_limit",
            errno.ENOSPC: "resource_limit", errno.ENOENT: "dependency_missing",
        }.get(error.errno, "dependency_missing" if result["operation"] == "seccomp_library" else "os_error")
        if type(error.errno) is int and 0 < error.errno < 4096:
            result["errno"] = error.errno
    except ValueError:
        result["reason"] = "verification_failed"
    except Exception:
        result["reason"] = "internal_error"
    os.write(2, json.dumps(result, sort_keys=True).encode("ascii"))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
