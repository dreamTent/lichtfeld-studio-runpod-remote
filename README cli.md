# CLI job runner

The dashboard is the usual way to run jobs. See [README.md](README.md) for setup and `python3 -m lichtfeld_runpod --ui`.

This file covers the **one-shot CLI**: create a single RunPod GPU, download a LichtFeld Studio build and a dataset archive from storage, train headless, upload the PLYs **and the full train log** plus a short report.

Progress is printed **on this machine** with timestamps, including download percent and training iteration / loss / splat count.

## Run

After setup from the main README (`pip install -e .` and `--init`):

```bash
python3 -m lichtfeld_runpod --config config.yaml
```

Fill in `.env` and `config.yaml` first. An SSH key is created at `~/.ssh/runpod_ed25519` if it is missing, and registered on the RunPod account.

## What you set

| Setting | Where | Meaning |
|---|---|---|
| RunPod API key | `.env` `RUNPOD_API_KEY` | Bearer token |
| GPU type | `runpod.gpu` | Exact id, e.g. `NVIDIA L40S` (one GPU, never MIG) |
| Cloud | `runpod.cloud` | `SECURE`, `COMMUNITY`, or `AUTO` |
| Disk / volume | `runpod.container_disk_gb`, `volume_gb` | Container disk + `/workspace` volume |
| Storage login | `.env` `SFTP_*` | Host, user, password |
| Transfer protocol | `storage.protocol` | `ftp` (port 21, default) or `sftp` (port 22) |
| Dataset archive | `storage.dataset_archive` | Path on the server, spaces allowed |
| Build archive | `storage.build_archive` | Packed LichtFeld tree (`.tar.gz`) |
| Result folder | `storage.result_dir` | Server folder for outputs |
| Dataset config | `lichtfeld.config` | JSON **inside** the extracted scene (relative to the COLMAP root). Empty = no `--config` |
| Max Gaussians | `lichtfeld.max_cap` | `--max-cap` |
| Sparsity / GUT | `lichtfeld.enable_sparsity`, `lichtfeld.gut` | CLI flags |
| Extra flags | `lichtfeld.extra_args` | Extra CLI tokens (dashboard: **Custom LichtFeld parameters**, e.g. `--export ply`) |
| Kill pod when done | `job.terminate_when_done` | Default true |

The scene root is detected automatically: a directory that contains `images/` and `sparse/` (COLMAP `cameras.txt` / `cameras.bin`).

## Progress

Local lines look like:

```
[2026-08-28 16:10:03 CEST] pod          cr41kg6zzfkcj4 gpu=NVIDIA L40S
[2026-08-28 16:10:40 CEST] download_da  dataset 52.1%  (8,123,456,789/15,582,942,208 bytes)
[2026-08-28 16:25:01 CEST] train        13100/45000 (29%)  loss=0.157  splats=10,000,000  remaining=03h:08m:22s
```

Downloads use **curl + FTP resume**, not Python `ftplib` (that client stalled around 3 MB/s on a 15 GB archive; curl held ~10–12 MB/s).

## What gets uploaded

Under `storage.result_dir`:

- `splat_*.ply` (root-level copies)
- the full `/workspace/output` tree under `output/`
- `REPORT.md`
- **`train.log`** (full LichtFeld log)
- `pipeline.log`, `train_summary.txt`, `version.txt`, `output_files.txt`

Runtime files (`pod_id`, SSH mux, netrc) go in `.run/` next to your config (gitignored).
