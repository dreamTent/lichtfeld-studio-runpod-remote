from __future__ import annotations

from .config import AppConfig, StorageConfig
from .storage import curl_url, staging_remote_path


def render_job_script(
    cfg: AppConfig,
    build_bytes: int | None,
    dataset_bytes: int | None,
    *,
    pod_id: str = "",
) -> str:
    lf = cfg.lichtfeld
    flags: list[str] = []
    if lf.headless:
        flags += ["--headless", "--no-splash"]
    if lf.gut:
        flags.append("--gut")
    if lf.enable_sparsity:
        flags.append("--enable-sparsity")
    if lf.max_cap is not None:
        flags += ["--max-cap", str(lf.max_cap)]
    if lf.iterations is not None:
        flags += ["--iter", str(lf.iterations)]
    if lf.strategy:
        flags += ["--strategy", lf.strategy]
    flags += list(lf.extra_args)
    flag_line = " ".join(_bash_quote(f) for f in flags)

    config_rel = lf.config
    build_url = curl_url(cfg.storage, cfg.storage.build_archive)
    dataset_url = curl_url(cfg.storage, cfg.storage.dataset_archive)
    result = cfg.storage.result_dir.rstrip("/")
    result_staging = staging_remote_path(result)
    extra_ftp = "--ftp-pasv" if cfg.storage.protocol == "ftp" else ""
    terminate = "1" if cfg.terminate_when_done else "0"
    rename_results = _rename_remote_cmd(cfg.storage, result_staging, result)

    return f"""#!/usr/bin/env bash
set -euo pipefail
ROOT=/workspace
LOGDIR="$ROOT/logs"
STATEDIR="$ROOT/state"
OUTDIR="$ROOT/output"
REPORTDIR="$ROOT/report"
DATASET_TAR="$ROOT/dataset/scene.tar"
BUILD_TGZ="$ROOT/lichtfeld-build.tar.gz"
RESULT_DIR={_bash_quote(result)}
RESULT_STAGING={_bash_quote(result_staging)}
BUILD_URL={_bash_quote(build_url)}
DATASET_URL={_bash_quote(dataset_url)}
BUILD_BYTES={build_bytes if build_bytes is not None else 0}
DATASET_BYTES={dataset_bytes if dataset_bytes is not None else 0}
CONFIG_REL={_bash_quote(config_rel)}
CURL_EXTRA={_bash_quote(extra_ftp)}
POD_ID={_bash_quote(pod_id)}
TERMINATE={terminate}

mkdir -p "$LOGDIR" "$STATEDIR" "$OUTDIR" "$REPORTDIR" "$ROOT/dataset" "$ROOT/extracted"
export CUDA_HOME=/usr/local/cuda-12.8
export PATH="/usr/local/cuda-12.8/bin:${{PATH}}"
export LD_LIBRARY_PATH="/workspace/LichtFeld-Studio/build:/workspace/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib:/usr/local/cuda-12.8/lib64:${{LD_LIBRARY_PATH:-}}"
ln -sfn /usr/local/cuda-12.8 /usr/local/cuda || true

log() {{ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOGDIR/pipeline.log"; }}
stage() {{ echo "$1" > "$STATEDIR/STAGE"; date +%s > "$STATEDIR/HEARTBEAT"; log "STAGE $1"; }}
done_stage() {{ [[ -f "$STATEDIR/$1.done" ]]; }}
mark_done() {{ date -u +%Y-%m-%dT%H:%M:%SZ > "$STATEDIR/$1.done"; }}

heartbeat_loop() {{
  while true; do
    date +%s > "$STATEDIR/HEARTBEAT" 2>/dev/null || true
    sleep 10
  done
}}
heartbeat_loop &
HB_PID=$!

fail() {{
  local rc=${{1:-1}}
  trap - ERR
  echo "$rc" > "$STATEDIR/train.exit" || true
  {{
    date -u +%Y-%m-%dT%H:%M:%SZ
    echo "failed rc=$rc"
  }} > "$STATEDIR/ERROR"
  echo error > "$STATEDIR/STAGE" || true
  log "pipeline failed rc=$rc"
  kill "$HB_PID" 2>/dev/null || true
  exit "$rc"
}}
trap 'fail $?' ERR

if ! command -v curl >/dev/null 2>&1; then
  log "curl missing; installing"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl
fi

curl_get() {{
  local url="$1" dest="$2" expect="$3"
  mkdir -p "$(dirname "$dest")"
  log "GET $url -> $dest"
  # shellcheck disable=SC2086
  curl -C - --netrc --connect-timeout 30 --retry 30 --retry-delay 5 \\
    $CURL_EXTRA --url "$url" -o "$dest"
  if [[ "$expect" -gt 0 ]]; then
    local got
    got=$(stat -c%s "$dest")
    if [[ "$got" -ne "$expect" ]]; then
      echo "size mismatch $dest got=$got expected=$expect" >&2
      fail 1
    fi
  fi
}}

if ! done_stage download_build; then
  stage download_build
  curl_get "$BUILD_URL" "$BUILD_TGZ" "$BUILD_BYTES"
  mark_done download_build
fi

if ! done_stage download_dataset; then
  stage download_dataset
  curl_get "$DATASET_URL" "$DATASET_TAR" "$DATASET_BYTES"
  mark_done download_dataset
fi

if ! done_stage extract_build; then
  stage extract_build
  tar -xzf "$BUILD_TGZ" -C "$ROOT"
  BIN=/workspace/LichtFeld-Studio/build/LichtFeld-Studio
  if [[ ! -x "$BIN" ]]; then
    echo "LichtFeld binary missing after extract" >&2
    exit 1
  fi
  "$BIN" --version | tee -a "$LOGDIR/pipeline.log"
  mark_done extract_build
fi

if ! done_stage extract_dataset; then
  stage extract_dataset
  python3 - <<'PY'
from pathlib import Path
import tarfile
import zipfile
src = Path("/workspace/dataset/scene.tar")
dest = Path("/workspace/extracted")
dest.mkdir(parents=True, exist_ok=True)
if zipfile.is_zipfile(src):
    with zipfile.ZipFile(src) as zf:
        zf.extractall(dest)
    print("extracted zip")
elif tarfile.is_tarfile(src):
    with tarfile.open(src) as tf:
        tf.extractall(dest)
    print("extracted tar")
else:
    raise SystemExit(f"dataset is neither zip nor tar: {{src}}")
PY
  mark_done extract_dataset
fi

find_data() {{
  python3 - <<'PY'
from pathlib import Path
root = Path("/workspace/extracted")
hits = []
for p in root.rglob("*"):
    if p.name in ("cameras.bin", "cameras.txt") and p.parent.name in ("0", "sparse"):
        scene = p.parent
        if scene.name == "0":
            scene = scene.parent
        if scene.name == "sparse":
            scene = scene.parent
        hits.append(scene)
uniq = []
for h in hits:
    if h not in uniq:
        uniq.append(h)
if not uniq:
    for d in root.rglob("sparse"):
        parent = d.parent
        if any((parent / n).exists() for n in ("images", "image", "imgs", "rgb")):
            uniq.append(parent)
if not uniq:
    raise SystemExit("could not locate COLMAP scene (images/ + sparse/)")
print(uniq[0])
PY
}}

DATA_PATH="$(find_data)"
log "Dataset path: $DATA_PATH"
CONFIG_ARG=()
if [[ -f /workspace/lichtfeld-config.json ]]; then
  CONFIG_ARG=(--config /workspace/lichtfeld-config.json)
  log "Using uploaded config /workspace/lichtfeld-config.json"
elif [[ -n "$CONFIG_REL" ]]; then
  CFG="$DATA_PATH/$CONFIG_REL"
  if [[ ! -f "$CFG" ]]; then
    echo "LichtFeld config not found: $CFG" >&2
    exit 1
  fi
  CONFIG_ARG=(--config "$CFG")
  log "Using config $CFG"
fi

BIN=/workspace/LichtFeld-Studio/build/LichtFeld-Studio
if ! done_stage train; then
  stage train
  set +e
  "$BIN" -d "$DATA_PATH" -o "$OUTDIR" {flag_line} "${{CONFIG_ARG[@]}}" --log-file "$LOGDIR/train.log"
  rc=$?
  set -e
  echo "$rc" > "$STATEDIR/train.exit"
  log "Training exited rc=$rc"
  if [[ $rc -ne 0 ]]; then
    tail -80 "$LOGDIR/train.log" | tee -a "$LOGDIR/pipeline.log" || true
    fail "$rc"
  fi
  mark_done train
fi

stage report
"$BIN" --version > "$REPORTDIR/version.txt" || true
cp -a "$LOGDIR/pipeline.log" "$REPORTDIR/" || true
if [[ -f "$LOGDIR/train.log" ]]; then
  cp -a "$LOGDIR/train.log" "$REPORTDIR/train.log"
  tail -c 500000 "$LOGDIR/train.log" > "$REPORTDIR/train_summary.txt" || true
fi
find "$OUTDIR" -type f -printf '%p %s\\n' > "$REPORTDIR/output_files.txt" || true
python3 - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
out = Path("/workspace/output")
files = []
if out.exists():
    for p in sorted(out.rglob("*")):
        if p.is_file():
            files.append(f"- `{{p.relative_to(out)}}`  ({{p.stat().st_size:,}} bytes)")
ver = Path("/workspace/report/version.txt").read_text(errors="replace").strip()
Path("/workspace/report/REPORT.md").write_text(
    "# LichtFeld Studio run\\n\\n"
    f"Date (UTC): {{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}}\\n\\n"
    f"## Software\\n- {{ver}}\\n\\n"
    "## Outputs\\n" + ("\\n".join(files) or "(none)") + "\\n",
    encoding="utf-8",
)
print("wrote REPORT.md")
PY

stage upload
# RESULT_URL is scheme://host:port/result_dir.upload/
RESULT_URL={_bash_quote(curl_url(cfg.storage, result_staging + "/"))}
upload_file() {{
  local src="$1" dest="$2"
  # shellcheck disable=SC2086
  curl --netrc --ftp-create-dirs $CURL_EXTRA -T "$src" "${{RESULT_URL}}${{dest}}"
}}

shopt -s nullglob
for f in "$OUTDIR"/*.ply "$REPORTDIR"/* "$LOGDIR/train.log" "$LOGDIR/pipeline.log"; do
  [[ -f "$f" ]] || continue
  upload_file "$f" "$(basename "$f")"
done
# nested ply / json
while IFS= read -r -d '' f; do
  rel="${{f#"$OUTDIR"/}}"
  upload_file "$f" "output/${{rel}}"
done < <(find "$OUTDIR" -type f \\( -name '*.ply' -o -name '*.json' -o -name '*.resume' \\) -print0)

log "renaming $RESULT_STAGING -> $RESULT_DIR"
{rename_results}

mark_done upload
stage done
log "Job complete"
kill "$HB_PID" 2>/dev/null || true
trap - ERR

if [[ "$TERMINATE" == "1" && -s /root/.runpod_api && -n "$POD_ID" ]]; then
  log "self-terminate $POD_ID"
  KEY=$(cat /root/.runpod_api)
  curl -sS -X DELETE "https://rest.runpod.io/v1/pods/${{POD_ID}}" \\
    -H "Authorization: Bearer ${{KEY}}" \\
    -H "Accept: application/json" \\
    -H "User-Agent: lichtfeld-runpod" || log "self-terminate request failed"
fi
"""


def _rename_remote_cmd(cfg: StorageConfig, src: str, dest: str) -> str:
    """curl command that renames a remote file or directory (uses $CURL_EXTRA)."""
    root = _bash_quote(curl_url(cfg, ""))
    extra = "$CURL_EXTRA"
    if cfg.protocol == "ftp":
        return (
            f"curl --netrc --fail {extra} "
            f"-Q {_bash_quote('-RNFR ' + src)} "
            f"-Q {_bash_quote('-RNTO ' + dest)} "
            f"{root}"
        )
    return (
        f"curl --netrc --fail {extra} "
        f"-Q {_bash_quote('-rename "' + src + '" "' + dest + '"')} "
        f"{root}"
    )


def _bash_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_build_script(
    cfg: AppConfig,
    *,
    pod_id: str = "",
    git_ref: str,
    cuda_arch: str,
    repo_url: str,
    archive_name: str,
) -> str:
    """Compile LichtFeld Studio on the pod, pack the tree, upload, self-terminate."""
    from .buildspec import CMAKE_INSTALLER_VERSION

    result = cfg.storage.result_dir.rstrip("/")
    result_staging = staging_remote_path(result)
    extra_ftp = "--ftp-pasv" if cfg.storage.protocol == "ftp" else ""
    terminate = "1" if cfg.terminate_when_done else "0"
    rename_results = _rename_remote_cmd(cfg.storage, result_staging, result)
    cmake_ver = CMAKE_INSTALLER_VERSION
    cmake_url = (
        f"https://github.com/Kitware/CMake/releases/download/v{cmake_ver}/"
        f"cmake-{cmake_ver}-linux-x86_64.sh"
    )
    gpu_label = cfg.runpod.gpu

    return f"""#!/usr/bin/env bash
set -euo pipefail
ROOT=/workspace
LOGDIR="$ROOT/logs"
STATEDIR="$ROOT/state"
REPORTDIR="$ROOT/report"
OUTDIR="$ROOT/out"
RESULT_DIR={_bash_quote(result)}
RESULT_STAGING={_bash_quote(result_staging)}
CURL_EXTRA={_bash_quote(extra_ftp)}
POD_ID={_bash_quote(pod_id)}
TERMINATE={terminate}
GIT_REF={_bash_quote(git_ref)}
CUDA_ARCH={_bash_quote(cuda_arch)}
REPO_URL={_bash_quote(repo_url)}
ARCHIVE_NAME={_bash_quote(archive_name)}
GPU_LABEL={_bash_quote(gpu_label)}
CMAKE_URL={_bash_quote(cmake_url)}
SRC="$ROOT/LichtFeld-Studio"
VCPKG_ROOT="$ROOT/vcpkg"
ARCHIVE="$OUTDIR/$ARCHIVE_NAME"

mkdir -p "$LOGDIR" "$STATEDIR" "$REPORTDIR" "$OUTDIR"
export DEBIAN_FRONTEND=noninteractive

log() {{ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOGDIR/pipeline.log"; }}
stage() {{ echo "$1" > "$STATEDIR/STAGE"; date +%s > "$STATEDIR/HEARTBEAT"; log "STAGE $1"; }}
done_stage() {{ [[ -f "$STATEDIR/$1.done" ]]; }}
mark_done() {{ date -u +%Y-%m-%dT%H:%M:%SZ > "$STATEDIR/$1.done"; }}

heartbeat_loop() {{
  while true; do
    date +%s > "$STATEDIR/HEARTBEAT" 2>/dev/null || true
    sleep 10
  done
}}
heartbeat_loop &
HB_PID=$!

self_terminate() {{
  if [[ "$TERMINATE" == "1" && -s /root/.runpod_api && -n "$POD_ID" ]]; then
    log "self-terminate $POD_ID"
    KEY=$(cat /root/.runpod_api)
    curl -sS -X DELETE "https://rest.runpod.io/v1/pods/${{POD_ID}}" \\
      -H "Authorization: Bearer ${{KEY}}" \\
      -H "Accept: application/json" \\
      -H "User-Agent: lichtfeld-runpod" || log "self-terminate request failed"
  fi
}}

fail() {{
  local rc=${{1:-1}}
  trap - ERR
  echo "$rc" > "$STATEDIR/train.exit" || true
  {{
    date -u +%Y-%m-%dT%H:%M:%SZ
    echo "failed rc=$rc"
  }} > "$STATEDIR/ERROR"
  echo error > "$STATEDIR/STAGE" || true
  log "pipeline failed rc=$rc"
  kill "$HB_PID" 2>/dev/null || true
  self_terminate
  exit "$rc"
}}
trap 'fail $?' ERR

if ! command -v curl >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq curl
fi

apt_retry() {{
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
}}

if ! done_stage apt; then
  stage apt
  apt_retry apt-get update -qq
  apt_retry apt-get install -y --no-install-recommends --fix-missing \\
    ca-certificates gpg wget git curl unzip zip tar pkg-config \\
    gcc-14 g++-14 ccache ninja-build \\
    python3 python3-dev python3-venv \\
    libxinerama-dev libxcursor-dev xorg-dev libglu1-mesa-dev \\
    libwayland-dev libxkbcommon-dev libegl-dev libdecor-0-dev \\
    libibus-1.0-dev libdbus-1-dev libsystemd-dev libgtk-3-dev \\
    nasm autoconf autoconf-archive automake libtool xz-utils
  update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100 \\
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

detect_cuda() {{
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
}}

CUDA_HOME="$(detect_cuda)" || {{
  log "nvcc not found"
  fail 1
}}
export CUDA_HOME
export CUDAToolkit_ROOT="$CUDA_HOME"
export CMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc"
export PATH="$CUDA_HOME/bin:/usr/local/bin:${{PATH}}"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${{LD_LIBRARY_PATH:-}}"
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
    printf '%s\\n' 'set(VCPKG_BUILD_TYPE release)' 'set(VCPKG_MAX_CONCURRENCY 8)' >> "$TRIPLET"
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
  cmake -B build -S . -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" \\
    -DCUDAToolkit_ROOT="$CUDA_HOME" \\
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \\
    -DBUILD_PYTHON_STUBS=OFF \\
    -DLFS_DEV_IMPORT_SOURCE_PYTHON=OFF \\
    -DLFS_DEV_IMPORT_SOURCE_RESOURCES=OFF \\
    -DCUDA_DEVICE_DEBUG=OFF \\
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
  rc=${{PIPESTATUS[0]}}
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
  tar --exclude='LichtFeld-Studio/.git' \\
      --exclude='LichtFeld-Studio/build/vcpkg_installed/vcpkg/blds' \\
      --exclude='LichtFeld-Studio/build/vcpkg_installed/vcpkg/pkgs' \\
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
ARCHIVE="${{1:-/workspace/lichtfeld-build.tar.gz}}"
DEST="${{2:-/workspace}}"
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
export PATH="${{CUDA_HOME:-/usr/local/cuda}}/bin:${{PATH}}"
export LD_LIBRARY_PATH="/workspace/LichtFeld-Studio/build:/workspace/LichtFeld-Studio/build/vcpkg_installed/x64-linux/lib:${{CUDA_HOME:-/usr/local/cuda}}/lib64:${{LD_LIBRARY_PATH:-}}"
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
    "# LichtFeld Studio build\\n\\n"
    f"Date (UTC): {{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}}\\n\\n"
    "## Software\\n"
    f"- {{ver}}\\n"
    f"- git: {{os.environ.get('GITDESC', '')}} ({{os.environ.get('GITSHA', '')}})\\n"
    f"- ref: {{os.environ.get('GIT_REF', '')}}\\n"
    f"- CUDA arch: sm_{{os.environ.get('CUDA_ARCH', '')}}\\n"
    f"- GPU: {{os.environ.get('GPU_LABEL', '')}}\\n"
    f"- archive: {{os.environ.get('ARCHIVE_NAME', '')}} ({{size:,}} bytes)\\n"
    "\\nExtract so the binary is at `LichtFeld-Studio/build/LichtFeld-Studio`.\\n",
    encoding="utf-8",
)
print("wrote REPORT.md")
PY

stage upload
RESULT_URL={_bash_quote(curl_url(cfg.storage, result_staging + "/"))}
upload_file() {{
  local src="$1" dest="$2"
  log "PUT $dest"
  # shellcheck disable=SC2086
  curl --netrc --ftp-create-dirs --connect-timeout 30 --retry 8 --retry-delay 5 \\
    -C - $CURL_EXTRA -T "$src" "${{RESULT_URL}}${{dest}}"
}}
upload_file "$ARCHIVE" "$ARCHIVE_NAME"
upload_file "$REPORTDIR/version.txt" "VERSION.txt"
upload_file "$REPORTDIR/restore.sh" "restore.sh"
upload_file "$LOGDIR/pipeline.log" "pipeline.log"
upload_file "$REPORTDIR/REPORT.md" "REPORT.md"

log "renaming $RESULT_STAGING -> $RESULT_DIR"
{rename_results}

mark_done upload
stage done
log "Build complete"
kill "$HB_PID" 2>/dev/null || true
trap - ERR
self_terminate
"""
