# Job and pod states

The dashboard shows a colored circle next to each job and each pod. Colors come from `lichtfeld_runpod/status.py`. Job **phases** are stored on the client; pipeline **stages** are written on the pod.

SSH is treated as fresh for **30 seconds** after the last successful poll (`HEARTBEAT_STALE_SECONDS`). After that the heartbeat is stale.

## Colors

| Circle | CSS class | Meaning |
|---|---|---|
| White | `white` | Pod this app did not start (foreign) |
| Green, blinking | `green_blink` | Job just created; not doing work yet |
| Blue, blinking | `blue_blink` | Job is waiting: upload, GPU, or SSH inject |
| Green (filled) | `green` | Running: SSH heartbeat is fresh |
| Teal (ring) | `teal` | Finished (`complete` or `completed (presumably)`) |
| Yellow | `yellow` | Controlled pod is still up, but SSH has gone stale |
| Red, blinking | `red_blink` | Job or pod is in an error state |
| Red (steady) | `red` | Controlled pod vanished before the job finished |

Blinking circles pulse. Running is a **filled green** disc; finished is a **hollow teal ring** so the two cannot be confused.

## Pod colors

Evaluated in this order (first match wins):

1. **White** — not controlled by this app (`controlled=false`).
2. **Blinking red** — the attached job has `phase=error` or a non-empty `error`.
3. **Teal ring** — the attached job has `phase=complete` (pod may already be gone).
4. **Red** — RunPod no longer lists the pod as `RUNNING` (status shown as `GONE` if it disappeared from the API).
5. **Green (filled)** — pod is running and SSH was OK within 30 seconds.
6. **Yellow** — pod is running but SSH is stale or missing.

A pod row is created from the RunPod API list. If a job still has a `pod_id` that is not in that list, a synthetic row is added with status `GONE`.

## Job phases and colors

| Phase | Color | When |
|---|---|---|
| `created` | Green, blinking | Job record exists; nothing started |
| `uploading_dataset` | Blue, blinking | Local folder is being tarred and uploaded to FTP |
| `waiting_for_pod` | Blue, blinking | Waiting for a GPU (create may be retrying) |
| `starting` | Blue, blinking | Pod exists; waiting for SSH and injecting the script |
| `running` | Follows the pod | Remote pipeline is in progress (see stages below) |
| `complete` | Teal ring | Finished. Message is `complete`, or `completed (presumably)` if inferred from FTP after SSH was lost |
| `error` | Blinking red | Failed locally, remote `ERROR` flag, or the pod disappeared before results showed up |

If `running` has no pod color yet, the job blinks blue.

`completed (presumably)` is still `phase=complete` (teal ring). It means `REPORT.md` was found in the job’s FTP result folder after SSH could not connect (typical if the pod uploaded and self-terminated while this client was offline). After a lost SSH connection the client probes FTP once; use **Reload** on the job to probe again.

## Remote pipeline stages

These are written to `/workspace/state/STAGE` on the pod. They are the job’s `stage` field while `phase` is `running`. They do not change the circle color by themselves.

| Stage | What the pod is doing |
|---|---|
| `download_build` | Fetching the LichtFeld build archive from FTP |
| `download_dataset` | Fetching the dataset archive from FTP |
| `extract_build` | Unpacking the build |
| `extract_dataset` | Unpacking the dataset |
| `train` | LichtFeld Studio training |
| `report` | Writing `REPORT.md` and related files |
| `upload` | Uploading PLYs, logs, and the report to FTP |
| `done` | Finished on the pod (then it may self-terminate) |

The dashboard subtitle shows `job.message` when set (progress text, `complete`, `completed (presumably)`, …), otherwise the phase name, then `last update: YYYY-MM-DD HH:MM:SS`. After SSH is lost that line also shows `(N connection errors)` since the last successful poll. SSH failures keep the last progress and append ` · connection error`; the raw SSH dump stays in the server log.

## Archive and delete (listing only)

The dashboard can **archive** or **remove** a job from the list. Neither action deletes:

- the FTP result folder
- local downloads under `results/`
- a GPU pod that is still running (use terminate when that exists)

**Reload** probes SSH (so the error count / last update changes) and re-lists the job’s FTP result folder. Automatic FTP checks after SSH loss happen only once.

**Archive** hides the job under the Archived tab; Unarchive brings it back. **Remove from list** deletes the client record (`.run/jobs/<id>.json` and that job’s SSH workdir). A running watch thread is stopped so it cannot recreate the listing.

