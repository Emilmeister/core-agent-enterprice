#!/usr/bin/env bash
# Disposable ARM64 kernel tests when the Docker host kernel lacks Landlock.
# Requires Docker only. This is a test fixture, never a production backend.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
image=${LANDLOCK_TEST_IMAGE:-core-agent-enterprice-agent:latest}
runner="core-landlock-vm-$$"
source_container="core-landlock-rootfs-$$"
log=${LANDLOCK_VM_LOG:-${TMPDIR:-/tmp}/core-landlock-vm-$$.log}
cleanup() {
  docker rm -f "$runner" "$source_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

test "$(docker image inspect "$image" --format '{{.Architecture}}')" = arm64
docker run -d --name "$runner" --memory=3g --cpus=3 \
  --mount "type=bind,source=$repo,target=/repo,readonly" \
  debian:trixie-slim sleep infinity >/dev/null
docker exec "$runner" sh -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends qemu-system-arm linux-image-arm64 busybox-static cpio kmod git ca-certificates > /install.log 2>&1'
docker exec "$runner" mkdir -p /vm/rootfs
docker create --name "$source_container" "$image" >/dev/null
docker export "$source_container" | docker exec -i "$runner" tar -C /vm/rootfs -xf -
docker rm "$source_container" >/dev/null

docker exec -i "$runner" sh <<'SETUP'
set -eu
grep -q '^CONFIG_SECURITY_LANDLOCK=y$' /boot/config-*
kernel_version=$(basename /lib/modules/*)
mkdir -p /vm/initramfs/bin /vm/initramfs/proc /vm/initramfs/sys \
  /vm/initramfs/dev /vm/initramfs/newroot /vm/initramfs/cgroup \
  /vm/initramfs/lib/modules/$kernel_version /vm/rootfs/app/tests
cp /bin/busybox /vm/initramfs/bin/busybox
cp /lib/modules/$kernel_version/modules.* /vm/initramfs/lib/modules/$kernel_version/
for module in virtio_pci 9pnet_virtio 9p; do
  modprobe --show-depends -S "$kernel_version" "$module" |
    awk '$1 == "insmod" {print $2}' | xargs -r cp --parents -t /vm/initramfs
done
# Add Git and its runtime libraries to the disposable exported root only.
cp -a /usr/bin/git /vm/rootfs/usr/bin/
cp -a /usr/lib/git-core /vm/rootfs/usr/lib/
cp -a /usr/share/git-core /vm/rootfs/usr/share/
ldd /usr/bin/git /usr/lib/git-core/git-remote-http |
  awk '/=> \// {print $3}' | sort -u | xargs -r cp -L --parents -t /vm/rootfs
cp -L --parents /lib/ld-linux-aarch64.so.1 /vm/rootfs

cat > /vm/initramfs/init <<'INIT'
#!/bin/busybox sh
set -eu
export PATH=/bin:/sbin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
ip link set lo up
mount -t cgroup2 cgroup2 /cgroup
echo +cpu +memory +pids > /cgroup/cgroup.subtree_control
mkdir /cgroup/guest
echo 1073741824 > /cgroup/guest/memory.max
echo 512 > /cgroup/guest/pids.max
echo 200000 100000 > /cgroup/guest/cpu.max
echo 1 > /cgroup/guest/cgroup.procs
modprobe virtio_pci
modprobe 9pnet_virtio
modprobe 9p
mount -t 9p -o trans=virtio,version=9p2000.L,ro rootfs /newroot
mount -t tmpfs -o mode=1777 tmpfs /newroot/tmp
mount -t tmpfs -o mode=0755 tmpfs /newroot/run
mount -t proc proc /newroot/proc
mount -t sysfs sysfs /newroot/sys
mount --bind /cgroup/guest /newroot/sys/fs/cgroup
mount -t devtmpfs devtmpfs /newroot/dev
mkdir -p /newroot/dev/pts
mount -t devpts devpts /newroot/dev/pts
mount -t 9p -o trans=virtio,version=9p2000.L,ro source /newroot/app/core_agent
mount -t 9p -o trans=virtio,version=9p2000.L,ro tests /newroot/app/tests
exec switch_root /newroot /bin/sh /vm-run.sh
INIT

cat > /vm/rootfs/vm-run.sh <<'RUN'
#!/bin/sh
export PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin
export CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1
export CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1
cd /app
exec /app/.venv/bin/python -u /vm-bootstrap.py
RUN

cat > /vm/rootfs/vm-bootstrap.py <<'PY'
import ctypes
import json
import os
import platform
import sys
import unittest

# A declared test outer filter, not evidence about the Cloud platform profile.
library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
library.seccomp_init.argtypes = [ctypes.c_uint32]
library.seccomp_init.restype = ctypes.c_void_p
library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
library.seccomp_load.argtypes = [ctypes.c_void_p]
library.seccomp_release.argtypes = [ctypes.c_void_p]
context = library.seccomp_init(0x7fff0000)
assert context
for name in (b'mount', b'unshare', b'setns'):
    assert library.seccomp_rule_add(context, 0x00050001,
        library.seccomp_syscall_resolve_name(name), 0) == 0
assert library.seccomp_load(context) == 0
library.seccomp_release(context)
libc = ctypes.CDLL(None, use_errno=True)
for capability in range(int(open('/proc/sys/kernel/cap_last_cap').read()) + 1):
    assert libc.prctl(24, capability, 0, 0, 0) == 0
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
sys.path.insert(0, '/app')
from core_agent.errors import SandboxDiagnostics

class NativeResult(unittest.TextTestResult):
    def addError(self, test, error):
        diagnostic = getattr(error[1], 'diagnostics', None)
        if type(diagnostic) is SandboxDiagnostics:
            print('NATIVE_DIAGNOSTICS=' + json.dumps(diagnostic.as_dict()), flush=True)
        super().addError(test, error)

abi = libc.syscall(444, 0, 0, 1)
assert abi >= 4, (abi, ctypes.get_errno())
print('NATIVE_FIXTURE', platform.release(), platform.machine(), 'pid', os.getpid(),
      'uid', os.getuid(), 'landlock_abi', abi, 'outer_filter=deny_mount_unshare_setns', flush=True)
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests.test_landlock_policy', 'tests.test_landlock_network',
    'tests.test_landlock_metadata', 'tests.test_sandbox_landlock',
    'tests.test_landlock_chat_files'])
result = unittest.TextTestRunner(verbosity=2, resultclass=NativeResult).run(suite)
status = 0 if result.wasSuccessful() else 1
print('LANDLOCK_VM_RESULT=' + str(status), flush=True)
os._exit(status)
PY
chmod +x /vm/initramfs/init /vm/rootfs/vm-run.sh
cd /vm/initramfs
find . -print0 | cpio --null -o --format=newc 2>/dev/null | gzip -1 > /vm/initrd.gz
cp /boot/vmlinuz-* /vm/vmlinuz
SETUP

set +e
docker exec "$runner" sh -c 'timeout 180 qemu-system-aarch64 -machine virt -cpu cortex-a72 -accel tcg,thread=multi -smp 2 -m 1536 -nographic -monitor none -nic none -no-reboot -kernel /vm/vmlinuz -initrd /vm/initrd.gz -append "console=ttyAMA0 rdinit=/init panic=1 lsm=landlock loglevel=3" -virtfs local,path=/vm/rootfs,mount_tag=rootfs,security_model=none,readonly=on -virtfs local,path=/repo/core_agent,mount_tag=source,security_model=none,readonly=on -virtfs local,path=/repo/tests,mount_tag=tests,security_model=none,readonly=on > /vm/tests.log 2>&1'
qemu_status=$?
set -e
docker cp "$runner:/vm/tests.log" "$log" >/dev/null
tr -d '\r' < "$log" | sed '/^LANDLOCK_VM_RESULT=/q'
printf 'Full guest log: %s\n' "$log"
# Exiting guest PID 1 deliberately ends this disposable VM. QEMU exit status
# alone cannot distinguish successful tests from a kernel panic or early exit.
test "$qemu_status" -eq 0
tr -d '\r' < "$log" | grep -qx 'LANDLOCK_VM_RESULT=0'
