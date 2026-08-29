from __future__ import annotations

from .config import AppConfig
from .storage import curl_url


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
    result = cfg.storage.result_dir
    extra_ftp = "--ftp-pasv" if cfg.storage.protocol == "ftp" else ""
    terminate = "1" if cfg.terminate_when_done else "0"

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
if [[ -n "$CONFIG_REL" ]]; then
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
# RESULT_URL is scheme://host:port/result_dir/
RESULT_URL={_bash_quote(curl_url(cfg.storage, result.rstrip("/") + "/"))}
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


def _bash_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
