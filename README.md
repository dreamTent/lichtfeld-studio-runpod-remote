# LichtFeld RunPod runner

Start a RunPod GPU, restore a LichtFeld Studio build, train headless, and upload results plus logs to your FTP/SFTP server. Pods terminate themselves after a successful upload unless you turn that off.

The usual way to run this is the **local dashboard**. A one-shot CLI job from `config.yaml` still works.

## What you need

- Python 3.11+
- `ssh`, `scp`, and `ssh-keygen` on this machine (OpenSSH)
- `curl` on this machine (used for FTP/SFTP transfers)
- A RunPod API key with full permissions
- An FTP or SFTP server with read/write access (for example a Hetzner Storage Box)
- Filezilla or another ftp software

On **Windows**, enable **OpenSSH Client** under Settings → Apps → Optional features. Windows 10+ already includes `curl.exe`. Use PowerShell, not Git Bash, so the app finds Microsoft OpenSSH rather than MSYS `ssh`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m lichtfeld_runpod --init
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python3 -m lichtfeld_runpod --init
```

`--init` writes `config.yaml` and `.env` if they do not already exist, and creates a local `datasets/` folder in the working directory.

Put secrets in **`.env`** (never commit this file):

- `RUNPOD_API_KEY`
- `SFTP_HOST`, `SFTP_USER`, `SFTP_PASSWORD`
- `STORAGE_PROTOCOL` — `ftp` (port 21) or `sftp` (port 22)

You can also enter the same values later in the dashboard under **Settings**.

An SSH key is created at `~/.ssh/runpod_ed25519` if it is missing, and registered on the RunPod account.

## FTP/SFTP layout

Paths are relative to the account’s login home (the directory you land in after connecting). Spaces in names are fine. The dashboard lists archives over **FTP** (port 21); SFTP can still transfer files if you set the paths yourself in `config.yaml`.

Use this top-level layout:

```
/
├── lichtfeld-builds/          # LichtFeld Studio binaries (you upload these)
│   └── lichtfeld-0.5.3-l40s-sm89-260828/
│       └── lichtfeld-0.5.3-l40s-sm89.tar.gz
├── lichtfeld-datasets/        # COLMAP scenes as archives
│   ├── my-scene.tar
│   └── other-scene.zip
└── lichtfeld-results/         # Job outputs (created automatically)
    └── job-name-abc123/
        ├── splat_*.ply
        ├── REPORT.md
        └── …
```

Accepted archive suffixes: `.tar.gz`, `.tgz`, `.tar`, `.zip`.

### Builds (`lichtfeld-builds/`)

Put packed LichtFeld Studio trees here. Nested folders are listed (up to 4 levels). If that directory is empty, the dashboard also offers archives in the FTP root whose path contains `build`.

A build archive must unpack so the binary is at `LichtFeld-Studio/build/LichtFeld-Studio` (the pod extracts into `/workspace`). Typical packing:

```bash
tar -czf lichtfeld-0.5.3-l40s-sm89.tar.gz LichtFeld-Studio
```

**Create build** in the dashboard is not implemented yet, so you upload these archives yourself.

### Datasets (`lichtfeld-datasets/`)

Each archive is one COLMAP scene (or a folder that contains one). After extract, the runner looks for a directory with `sparse/` (COLMAP `cameras.bin` or `cameras.txt`) and an image folder named `images/`, `image/`, `imgs/`, or `rgb/`. Nested layouts are fine.

The dashboard lists archives under `lichtfeld-datasets/` plus any archive sitting in the FTP root. A local folder or archive chosen in **New job** is uploaded to `lichtfeld-datasets/<original-name>-<job-id>.tar` (or the original archive suffix). On this machine, put scenes in the `datasets/` folder (created at startup) or browse elsewhere.

An optional LichtFeld JSON config lives **inside** the scene (next to `images/` and `sparse/`). The path you enter in the job form is relative to that scene root.

### Results (`lichtfeld-results/`)

Created on upload. The default folder is `lichtfeld-results/<job-name>-<job-id>` unless you set a different path. Contents are listed under [What gets uploaded](#what-gets-uploaded).

## Start the dashboard

```powershell
python -m lichtfeld_runpod --ui
```

Linux / macOS: `python3 -m lichtfeld_runpod --ui`.

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Optional: `--host` and `--port` (defaults `127.0.0.1` and `8765`).

Circle colors and job/pod phases are listed in [STATES.md](STATES.md). **Discard pod** on a job or a foreign pod terminates that GPU on RunPod.

`--ui` only starts the local app. It does not create a GPU pod until you submit a job.

### Settings

Open **Settings** and save your RunPod API key and storage login. Values are written to `.env` in the working directory.

### New job

Open **New job** and choose:

- GPU type and cloud (`SECURE`, `COMMUNITY`, or `AUTO`)
- RunPod **image** (defaults to `runpod.image` in `config.yaml`; change it per job if you need another template)
- A LichtFeld **build** from `lichtfeld-builds/` ([layout](#ftpsftp-layout))
- A **dataset**: an existing archive under `lichtfeld-datasets/`, or a local folder/archive
  - Local picks use native **Browse folder…** / **Browse archive…** dialogs (the in-app list is a fallback) and default to the `datasets/` folder next to the app
  - If you pick an existing `.tar` / `.tar.gz` / `.zip`, you can **upload it as-is** instead of copying it into the job folder first
- Optional LichtFeld config path (relative to the COLMAP scene root: the folder with `images/` and `sparse/`)
- Or a **separate local JSON config** uploaded to the pod (used instead of a config inside the dataset archive)
- Result folder on FTP
- Optional **Override LichtFeld settings** (max Gaussians, sparsity, GUT). Leave this off to use only the LichtFeld config file and the binary defaults
- Whether to download results to this machine when done
- Whether the pod should terminate after the FTP upload (on by default)

While a local dataset is uploading, **Abort upload** on the job detail stops the FTP transfer and cancels that job.

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
