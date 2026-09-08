# verda-sniper

Books an RTX A6000 on Verda the moment one frees up. Headless — no browser,
no laptop, no login session. Runs on free GitHub Actions.

Verda's A6000 stock appears and is gone again in under two minutes, often at
night. Watching by hand does not work. This polls the REST API every 10
seconds and books with a single API call.

## Setup

1. Push this to a **public** GitHub repo (public = unlimited Actions minutes).
   Never commit credentials — they live in encrypted repo secrets.
2. console.verda.com -> Credentials -> create a REST API key.
3. Repo -> Settings -> Secrets and variables -> Actions -> add:
   - `VERDA_CLIENT_ID`
   - `VERDA_CLIENT_SECRET`
4. Actions tab -> `snipe` -> Run workflow, tick **dry_run**, to confirm the
   credentials and image/key lookups work without spending anything.
5. Untick dry_run and let the schedule take over.

When it books, it opens a GitHub issue with the instance ID and IP. GitHub
emails you automatically for issues on your own repo.

## Safety

- Refuses to create a second instance if the project already has one, so
  overlapping runs cannot double-spend.
- Re-checks for an existing instance immediately before spending.
- On-demand only, 1 GPU, never spot.
- If it cannot list your instances, it aborts rather than booking blind.

## Config

Defaults suit AC2. Override via env in the workflow:

| Var | Default |
|---|---|
| `VERDA_INSTANCE_TYPE` | `1A6000.10V` |
| `VERDA_IMAGE_NAME` | `Ubuntu 24.04 + CUDA 12.8 Open + Docker` |
| `VERDA_SSH_KEY_NAME` | `ac2-verda` |
| `VERDA_HOSTNAME` | `ac2-prod` |
| `VERDA_OS_DISK_GB` | `100` |

100 GB, not 50: the CUDA image plus ~28 GB of models left only 555 MB free on
the last rebuild, and `verda_bootstrap.sh` refuses to run below 32 GB free.

## Run locally

    VERDA_CLIENT_ID=... VERDA_CLIENT_SECRET=... VERDA_DRY_RUN=1 \
      VERDA_RUN_SECONDS=30 python3 verda_sniper.py
