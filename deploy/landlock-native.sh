#!/usr/bin/env bash
# Mandatory native gate; no host changes and no fallback when Landlock is absent.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
image=${LANDLOCK_TEST_IMAGE:?Set LANDLOCK_TEST_IMAGE to the candidate image}
case "$(docker info --format '{{.OSType}}/{{.Architecture}}')" in
  linux/x86_64|linux/amd64) arch=amd64 ;;
  linux/aarch64|linux/arm64) arch=arm64 ;;
  *) printf 'Native Linux amd64 or arm64 Docker daemon required\n' >&2; exit 1 ;;
esac
test "$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')" = "linux/$arch"
test "$(docker image inspect "$image" --format '{{.Config.User}}')" = agent

# The vendored Moby baseline denies namespace creation with zero capabilities.
# Mount tests only: runtime code and dependencies must come from the candidate.
# uv run retains a parent, so select with uv and exec Python to preserve PID 1.
docker run --rm --platform "linux/$arch" --network none --cgroupns private \
  --read-only --cap-drop ALL --security-opt no-new-privileges=true \
  --security-opt "seccomp=$repo/deploy/security/upstream/moby-default.json" \
  --memory 4g --memory-swap 4g --cpus 2 --pids-limit 512 \
  --tmpfs /tmp:rw,nosuid,nodev,exec,mode=1777,size=1g \
  --mount "type=bind,source=$repo/tests,target=/app/tests,readonly" \
  --env CORE_AGENT_REQUIRE_LANDLOCK_TESTS=1 \
  --env CORE_AGENT_REQUIRE_NATIVE_SECCOMP_TESTS=1 \
  --env PYTHONDONTWRITEBYTECODE=1 --env UV_CACHE_DIR=/tmp/uv-cache \
  --entrypoint sh "$image" -ec '
    exec "$(uv python find --offline --no-python-downloads /app/.venv/bin/python)" \
      -m unittest tests.test_landlock_policy tests.test_landlock_network \
      tests.test_landlock_metadata tests.test_sandbox_landlock tests.test_landlock_chat_files -v
  '
