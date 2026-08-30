#!/usr/bin/env bash
# LichtFeld Studio compile pipeline. Runs on the GPU pod.
#
# Job parameters come from /workspace/build.env (written by the local app).
# To use a different pipeline, put a remote_build.sh next to config.yaml;
# that file is uploaded instead of this packaged default.
set -euo pipefail

ROOT=/workspace
ENV_FILE="${BUILD_ENV:-$ROOT/build.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

LOGDIR="$ROOT/logs"
STATEDIR="$ROOT/state"
REPORTDIR="$ROOT/report"
OUTDIR="$ROOT/out"
SRC="$ROOT/LichtFeld-Studio"
VCPKG_ROOT="$ROOT/vcpkg"
ARCHIVE="$OUTDIR/${ARCHIVE_NAME:?ARCHIVE_NAME is required}"

: "${RESULT_DIR:?}"
: "${RESULT_STAGING:?}"
: "${RESULT_URL:?}"
: "${STORAGE_ROOT_URL:?}"
: "${STORAGE_PROTOCOL:=ftp}"
: "${CURL_EXTRA:=}"
: "${POD_ID:=}"
: "${TERMINATE:=1}"
: "${GIT_REF:?}"
: "${CUDA_ARCH:?}"
: "${REPO_URL:?}"
: "${GPU_LABEL:=}"
: "${CMAKE_URL:=https://github.com/Kitware/CMake/releases/download/v3.31.6/cmake-3.31.6-linux-x86_64.sh}"

mkdir -p "$LOGDIR" "$STATEDIR" "$REPORTDIR" "$OUTDIR"
export DEBIAN_FRONTEND=noninteractive

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOGDIR/pipeline.log"; }
stage() { echo "$1" > "$STATEDIR/STAGE"; date +%s > "$STATEDIR/HEARTBEAT"; log "STAGE $1"; }
done_stage() { [[ -f "$STATEDIR/$1.done" ]]; }
mark_done() { date -u +%Y-%m-%dT%H:%M:%SZ > "$STATEDIR/$1.done"; }

heartbeat_loop() {
  while true; do
    date +%s > "$STATEDIR/HEARTBEAT" 2>/dev/null || true
    sleep 10
  done
}
heartbeat_loop &
HB_PID=$!

self_terminate() {
  if [[ "$TERMINATE" == "1" && -s /root/.runpod_api && -n "$POD_ID" ]]; then
    log "self-terminate $POD_ID"
    KEY=$(cat /root/.runpod_api)
    curl -sS -X DELETE "https://rest.runpod.io/v1/pods/${POD_ID}" \
      -H "Authorization: Bearer ${KEY}" \
      -H "Accept: application/json" \
      -H "User-Agent: lichtfeld-runpod" || log "self-terminate request failed"
  fi
}

fail() {
  local rc=${1:-1}
  trap - ERR
  echo "$rc" > "$STATEDIR/train.exit" || true
  {
    date -u +%Y-%m-%dT%H:%M:%SZ
    echo "failed rc=$rc"
  } > "$STATEDIR/ERROR"
  echo error > "$STATEDIR/STAGE" || true
  log "pipeline failed rc=$rc"
  kill "$HB_PID" 2>/dev/null || true
  self_terminate
  exit "$rc"
}
trap 'fail $?' ERR

if ! command -v curl >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq curl
fi

apt_retry() {
  local n=0
  until "$@"; do
    n=$((n+1))
    if [[ $n -ge 5 ]]; then
      return 1
    fi
    log "command failed, retry $n/5: $*"
    sleep 8
    apt-get update -qq || true
  done
}

if ! done_stage apt; then
  stage apt
  apt_retry apt-get update -qq
  apt_retry apt-get install -y --no-install-recommends --fix-missing \
    ca-certificates gpg wget git curl unzip zip tar pkg-config \
    gcc-14 g++-14 ccache ninja-build \
    python3 python3-dev python3-venv \
    libxinerama-dev libxcursor-dev xorg-dev libglu1-mesa-dev \
    libwayland-dev libxkbcommon-dev libegl-dev libdecor-0-dev \
    libibus-1.0-dev libdbus-1-dev libsystemd-dev libgtk-3-dev \
    nasm autoconf autoconf-archive automake libtool xz-utils
  update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100 \
    --slave /usr/bin/g++ g++ /usr/bin/g++-14 || true
  export CC=gcc-14 CXX=g++-14
  mark_done apt
fi
export CC=gcc-14 CXX=g++-14

if ! done_stage cmake; then
  stage cmake
  need_cmake=1
  if command -v cmake >/dev/null 2>&1; then
    if python3 -c 'import subprocess,sys
p=subprocess.check_output(["cmake","--version"],text=True).split()[2].split(".")
sys.exit(0 if (int(p[0]),int(p[1]))>=(3,30) else 1)'; then
      need_cmake=0
      log "cmake $(cmake --version | head -1) already ok"
    fi
  fi
  if [[ "$need_cmake" == "1" ]]; then
    log "installing CMake from $CMAKE_URL"
    curl -fsSL --retry 8 --retry-delay 3 -o /tmp/cmake-installer.sh "$CMAKE_URL"
    sh /tmp/cmake-installer.sh --skip-license --prefix=/usr/local
  fi
  cmake --version | tee -a "$LOGDIR/pipeline.log"
  mark_done cmake
fi

detect_cuda() {
  local d
  for d in /usr/local/cuda-12.8 /usr/local/cuda-12.9 /usr/local/cuda-13.0 /usr/local/cuda; do
    if [[ -x "$d/bin/nvcc" ]]; then
      printf '%s' "$d"
      return 0
    fi
  done
  if command -v nvcc >/dev/null 2>&1; then
    dirname "$(dirname "$(command -v nvcc)")"
    return 0
  fi
  return 1
}

CUDA_HOME="$(detect_cuda)" || {
  log "nvcc not found"
  fail 1
}
export CUDA_HOME
export CUDAToolkit_ROOT="$CUDA_HOME"
export CMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc"
export PATH="$CUDA_HOME/bin:/usr/local/bin:${PATH}"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
ln -sfn "$CUDA_HOME" /usr/local/cuda || true
log "CUDA_HOME=$CUDA_HOME nvcc=$("$CUDA_HOME/bin/nvcc" --version | tail -1)"

if ! done_stage vcpkg; then
  stage vcpkg
  export VCPKG_ROOT
  export VCPKG_FORCE_SYSTEM_BINARIES=1
  export VCPKG_BUILD_TYPE=release
  if [[ ! -x "$VCPKG_ROOT/vcpkg" ]]; then
    git clone https://github.com/microsoft/vcpkg.git "$VCPKG_ROOT"
    "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
  fi
  TRIPLET="$VCPKG_ROOT/triplets/x64-linux.cmake"
  if [[ -f "$TRIPLET" ]] && ! grep -q VCPKG_BUILD_TYPE "$TRIPLET"; then
    printf '%s\n' 'set(VCPKG_BUILD_TYPE release)' 'set(VCPKG_MAX_CONCURRENCY 8)' >> "$TRIPLET"
  fi
  mark_done vcpkg
fi
export VCPKG_ROOT
export VCPKG_FORCE_SYSTEM_BINARIES=1
export VCPKG_BUILD_TYPE=release

if ! done_stage clone; then
  stage clone
  rm -rf "$SRC"
  git clone --branch "$GIT_REF" --depth 1 --recurse-submodules "$REPO_URL" "$SRC"
  git -C "$SRC" rev-parse --short HEAD | tee "$STATEDIR/gitsha"
  git -C "$SRC" describe --tags --always | tee "$STATEDIR/gitdesc"
  mark_done clone
fi

if ! done_stage configure; then
  stage configure
  cd "$SRC"
  cmake -B build -S . -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" \
    -DCUDAToolkit_ROOT="$CUDA_HOME" \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \
    -DBUILD_PYTHON_STUBS=OFF \
    -DLFS_DEV_IMPORT_SOURCE_PYTHON=OFF \
    -DLFS_DEV_IMPORT_SOURCE_RESOURCES=OFF \
    -DCUDA_DEVICE_DEBUG=OFF \
    -DLFS_ENFORCE_LINUX_GUI_BACKENDS=OFF
  rm -rf build/vcpkg_installed/vcpkg/blds build/vcpkg_installed/vcpkg/pkgs || true
  rm -rf "$VCPKG_ROOT/buildtrees" "$VCPKG_ROOT/downloads" "$VCPKG_ROOT/packages" || true
  mark_done configure
fi

if ! done_stage compile; then
  stage compile
  cd "$SRC"
  JOBS=$(nproc)
  if [[ "$JOBS" -gt 32 ]]; then JOBS=32; fi
  log "ninja -j $JOBS"
  set +e
  cmake --build build -j "$JOBS" 2>&1 | tee -a "$LOGDIR/pipeline.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [[ $rc -ne 0 ]]; then
    log "compile failed rc=$rc"
    fail "$rc"
  fi
  BIN="$SRC/build/LichtFeld-Studio"
  if [[ ! -x "$BIN" ]]; then
    log "binary missing after compile"
    fail 1
  fi
  "$BIN" --version | tee "$REPORTDIR/version.txt" | tee -a "$LOGDIR/pipeline.log"
  mark_done compile
fi

if ! done_stage pack; then
  stage pack
  BIN="$SRC/build/LichtFeld-Studio"
  if [[ ! -x "$BIN" ]]; then
    log "binary missing before pack"
    fail 1
  fi
  rm -rf "$SRC/build/vcpkg_installed/vcpkg/blds" "$SRC/build/vcpkg_installed/vcpkg/pkgs" || true
  tar --exclude='LichtFeld-Studio/.git' \
      --exclude='LichtFeld-Studio/build/vcpkg_installed/vcpkg/blds' \
      --exclude='LichtFeld-Studio/build/vcpkg_installed/vcpkg/pkgs' \
      -czf "$ARCHIVE" -C "$ROOT" LichtFeld-Studio
  stat -c%s "$ARCHIVE" | tee "$STATEDIR/archive_bytes"
  log "packed $ARCHIVE ($(stat -c%s "$ARCHIVE") bytes)"
  mark_done pack
fi

stage report
BIN="$SRC/build/LichtFeld-Studio"
"$BIN" --version > "$REPORTDIR/version.txt" || true
cp -a "$LOGDIR/pipeline.log" "$REPORTDIR/" || true
GITSHA=$(cat "$STATEDIR/gitsha" 2>/dev/null || echo unknown)
GITDESC=$(cat "$STATEDIR/gitdesc" 2>/dev/null || echo "$GIT_REF")
cat > "$REPORTDIR/restore.sh" <<'RESTORE'
#!/usr/bin/env bash
# Restore a packed LichtFeld Studio tree onto /workspace.
set -euo pipefail
ARCHIVE="${1:-/workspace/lichtfeld-build.tar.gz}"
DEST="${2:-/workspace}"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "missing archive: $ARCHIVE" >&2
  exit 1
fi
mkdir -p "$DEST"
tar -xzf "$ARCHIVE" -C "$DEST"
if [[ -x /usr/local/cuda-12.8/bin/nvcc ]]; then
  ln -sfn /usr/local/cuda-12.8 /usr/local/cuda || true
  export CUDA_HOME=/usr/local/cuda-12.8
elif [[ -x /usr/local/cuda/bin/nvcc ]]; then
  export CUDA_HOME=/usr/local/cuda
fi
export PATH="${CUDA_HOME:-/usr/local/cuda}/bin:${PATH}"
export LD_LIBRARY_PATH="/workspace/LichtFeld-Studio/build:/workspace/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib:${CUDA_HOME:-/usr/local/cuda}/lib64:${LD_LIBRARY_PATH:-}"
BIN=/workspace/LichtFeld-Studio/build/LichtFeld-Studio
if [[ ! -x "$BIN" ]]; then
  echo "binary not found at $BIN" >&2
  exit 1
fi
"$BIN" --version
echo "Restore OK."
RESTORE
chmod +x "$REPORTDIR/restore.sh"
export GITSHA GITDESC
python3 - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import os
ver = Path("/workspace/report/version.txt").read_text(errors="replace").strip()
archive = Path("/workspace/out") / os.environ.get("ARCHIVE_NAME", "")
size = archive.stat().st_size if archive.is_file() else 0
Path("/workspace/report/REPORT.md").write_text(
    "# LichtFeld Studio build\n\n"
    f"Date (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}\n\n"
    "## Software\n"
    f"- {ver}\n"
    f"- git: {os.environ.get('GITDESC', '')} ({os.environ.get('GITSHA', '')})\n"
    f"- ref: {os.environ.get('GIT_REF', '')}\n"
    f"- CUDA arch: sm_{os.environ.get('CUDA_ARCH', '')}\n"
    f"- GPU: {os.environ.get('GPU_LABEL', '')}\n"
    f"- archive: {os.environ.get('ARCHIVE_NAME', '')} ({size:,} bytes)\n"
    "\nExtract so the binary is at `LichtFeld-Studio/build/LichtFeld-Studio`.\n",
    encoding="utf-8",
)
print("wrote REPORT.md")
PY

stage upload
upload_file() {
  local src="$1" dest="$2"
  log "PUT $dest"
  # shellcheck disable=SC2086
  curl --netrc --ftp-create-dirs --connect-timeout 30 --retry 8 --retry-delay 5 \
    -C - $CURL_EXTRA -T "$src" "${RESULT_URL}${dest}"
}
upload_file "$ARCHIVE" "$ARCHIVE_NAME"
upload_file "$REPORTDIR/version.txt" "VERSION.txt"
upload_file "$REPORTDIR/restore.sh" "restore.sh"
upload_file "$LOGDIR/pipeline.log" "pipeline.log"
upload_file "$REPORTDIR/REPORT.md" "REPORT.md"

log "renaming $RESULT_STAGING -> $RESULT_DIR"
# shellcheck disable=SC2086
if [[ "$STORAGE_PROTOCOL" == "ftp" ]]; then
  curl --netrc --fail $CURL_EXTRA \
    -Q "-RNFR ${RESULT_STAGING}" \
    -Q "-RNTO ${RESULT_DIR}" \
    "$STORAGE_ROOT_URL"
else
  curl --netrc --fail $CURL_EXTRA \
    -Q "-rename \"${RESULT_STAGING}\" \"${RESULT_DIR}\"" \
    "$STORAGE_ROOT_URL"
fi

mark_done upload
stage done
log "Build complete"
kill "$HB_PID" 2>/dev/null || true
trap - ERR
self_terminate
