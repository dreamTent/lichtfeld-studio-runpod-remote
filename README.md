# LichtFeld RunPod runner

Start a RunPod GPU, restore a LichtFeld Studio build, train headless, and upload results plus logs to your FTP/SFTP server. Pods terminate themselves after a successful upload unless you turn that off.

The usual way to run this is the **local dashboard**. A one-shot CLI job from `config.yaml` still works.

## What you need

- Python 3.11+
- `ssh` and `scp` on this machine
- A RunPod API key with full permissions
- An FTP or SFTP server with read/write access (for example a Hetzner Storage Box)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -e .
python3 -m lichtfeld_runpod --init
```

`--init` writes `config.yaml` and `.env` if they do not already exist.

Put secrets in **`.env`** (never commit this file):

- `RUNPOD_API_KEY`
- `SFTP_HOST`, `SFTP_USER`, `SFTP_PASSWORD`
- `STORAGE_PROTOCOL` — `ftp` (port 21) or `sftp` (port 22)

You can also enter the same values later in the dashboard under **Settings**.

An SSH key is created at `~/.ssh/runpod_ed25519` if it is missing, and registered on the RunPod account.

## Start the dashboard

```bash
python3 -m lichtfeld_runpod --ui
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Optional: `--host` and `--port` (defaults `127.0.0.1` and `8765`).

`--ui` only starts the local app. It does not create a GPU pod until you submit a job.

### Settings

Open **Settings** and save your RunPod API key and storage login. Values are written to `.env` in the working directory.

### New job

Open **New job** and choose:

- GPU type and cloud (`SECURE`, `COMMUNITY`, or `AUTO`)
- A LichtFeld **build** already on the FTP server
- A **dataset**: an existing FTP archive, or a local folder (tarred and uploaded)
- Optional LichtFeld config path (relative to the COLMAP scene root: the folder with `images/` and `sparse/`)
- Result folder on FTP, max Gaussians, sparsity / GUT
- Whether to download results to this machine when done
- Whether the pod should terminate after the FTP upload (on by default)

**Create build** in the nav is not implemented yet.

## What a job does

1. Optionally tars a local dataset and uploads it to FTP
2. Creates a RunPod GPU
3. Copies the remote job script over SSH
4. Downloads the LichtFeld build and dataset onto the pod from FTP
5. Runs LichtFeld Studio headless (with your config file if you set one)
6. Uploads PLYs, logs, and a short report to the result folder on FTP
7. Optionally downloads that folder to this machine
8. Terminates the pod if that option is on

## One-shot CLI (no dashboard)

Edit `config.yaml` (archive paths, GPU, LichtFeld flags) and `.env`, then:

```bash
python3 -m lichtfeld_runpod --config config.yaml
```

Progress prints in the terminal (download percent, training iteration / loss / splat count). CLI-specific settings are documented in [README cli.md](README%20cli.md).

## What gets uploaded

Under the job’s result folder on the server:

- `splat_*.ply` (and copies under `output/`)
- `REPORT.md`
- `train.log` (full LichtFeld log)
- `pipeline.log`, `train_summary.txt`, `version.txt`, `output_files.txt`

Runtime state (`pod_id`, SSH mux, job records) lives in `.run/` next to your config (gitignored).
