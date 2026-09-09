#!/usr/bin/env python3
"""Poll Verda for an RTX A6000 and book it the instant one appears.

Designed to run headless (GitHub Actions, cron, systemd). No browser, no login
session. Talks to the Verda/DataCrunch REST API directly.

Safety: it refuses to create a second instance if the project already has one,
so overlapping runs can never double-spend.

Env:
  VERDA_CLIENT_ID, VERDA_CLIENT_SECRET   REST API credentials (required)
  VERDA_INSTANCE_TYPE   default 1A6000.10V  (1x RTX A6000 48GB)
  VERDA_IMAGE_NAME      substring match against the OS image name
  VERDA_SSH_KEY_NAME    default ac2-verda
  VERDA_HOSTNAME        default ac2-prod
  VERDA_OS_DISK_GB      default 100
  VERDA_POLL_SECONDS    default 10
  VERDA_RUN_SECONDS     default 540 (how long one invocation loops)
  VERDA_DRY_RUN         "1" to log what it would book without booking
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("VERDA_API", "https://api.verda.com/v1")

INSTANCE_TYPE = os.environ.get("VERDA_INSTANCE_TYPE", "1A6000.10V")
IMAGE_NAME = os.environ.get("VERDA_IMAGE_NAME", "Ubuntu 24.04 + CUDA 12.8 Open + Docker")
SSH_KEY_NAME = os.environ.get("VERDA_SSH_KEY_NAME", "ac2-verda")
HOSTNAME = os.environ.get("VERDA_HOSTNAME", "ac2-prod")
STARTUP_SCRIPT = os.environ.get("VERDA_STARTUP_SCRIPT", "startup.sh")
STARTUP_NAME = os.environ.get("VERDA_STARTUP_NAME", "ac2-autoprovision")
OS_DISK_GB = int(os.environ.get("VERDA_OS_DISK_GB", "100"))
POLL_SECONDS = float(os.environ.get("VERDA_POLL_SECONDS", "5"))
RUN_SECONDS = float(os.environ.get("VERDA_RUN_SECONDS", "540"))
DRY_RUN = os.environ.get("VERDA_DRY_RUN") == "1"
# Fire the real POST once, even with no stock, to prove the payload is accepted.
PROBE = os.environ.get("VERDA_PROBE") == "1"

_LAST_SEEN = None


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class Verda:
    def __init__(self, client_id, client_secret):
        self._id = client_id
        self._secret = client_secret
        self._token = None
        self._token_expires = 0.0

    def _call(self, path, token=None, data=None, method=None):
        url = f"{API}{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, method=method or ("POST" if body else "GET")
        )
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else None

    def token(self):
        # Refresh a minute before expiry so a long loop never dies mid-poll.
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        r = self._call("/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": self._id,
            "client_secret": self._secret,
        })
        self._token = r["access_token"]
        self._token_expires = time.time() + float(r.get("expires_in", 3600))
        return self._token

    def get(self, path):
        return self._call(path, token=self.token())

    def post(self, path, data):
        return self._call(path, token=self.token(), data=data)


def locations_with(payload, itype):
    """Return location codes whose availability list contains itype."""
    found = []
    entries = payload if isinstance(payload, list) else [payload]
    for e in entries:
        if not isinstance(e, dict):
            continue
        loc = e.get("location_code") or e.get("location") or e.get("code")
        avail = (e.get("availabilities") or e.get("available")
                 or e.get("instance_types") or [])
        if isinstance(avail, list) and itype in avail:
            found.append(loc)
    return found


def all_types(payload):
    """Every instance type the availability feed currently reports, any location."""
    out = set()
    for e in (payload if isinstance(payload, list) else []):
        if isinstance(e, dict):
            for x in (e.get("availabilities") or e.get("available")
                      or e.get("instance_types") or []):
                out.add(x)
    return sorted(out)


def check_feed_alive(v):
    """Fail loudly if the availability feed reports nothing at all.

    A `?is_spot=false` filter once made this endpoint return nothing, and
    "no types anywhere" is indistinguishable from "no stock" - so the watcher
    polled blind for 32 hours while the console showed an A6000 free. Verda
    always has something available somewhere, so an empty feed means the query
    is wrong. Treat it as a hard error, never as scarcity.
    """
    payload = v.get("/instance-availability")
    types = all_types(payload)
    if not types:
        log("FEED DEAD: /instance-availability reported zero instance types "
            "across all locations. That is a broken query, not sold-out stock. "
            f"Raw payload: {json.dumps(payload)[:400]}")
        return False, types
    log(f"feed healthy: {len(types)} instance types available right now: {types}")
    return True, types


def ensure_startup_script(v):
    """Upload the provisioning script to Verda and return its id.

    Attached to the booking so a freshly caught box installs Ollama, pulls the
    four models and warms them on its own. Re-uploads when the local file
    changes, so editing startup.sh is all it takes to change what a new box
    does. Returns None on failure - a box booked without provisioning still
    beats no box.
    """
    path = Path(STARTUP_SCRIPT) if False else None
    try:
        with open(STARTUP_SCRIPT) as fh:
            body = fh.read()
    except OSError as e:
        log(f"WARN no startup script ({e}); booking without provisioning")
        return None
    try:
        existing = v.get("/scripts") or []
        for s in existing:
            if s.get("name") == STARTUP_NAME:
                if (s.get("script") or "") == body:
                    log(f"startup script up to date -> {s.get('id')}")
                    return s.get("id")
                log("startup script changed; replacing")
                try:
                    v._call(f"/scripts/{s.get('id')}", token=v.token(), method="DELETE")
                except Exception as e:
                    log(f"WARN could not delete old script: {e}")
                break
        created = v.post("/scripts", {"name": STARTUP_NAME, "script": body})
        sid = created if isinstance(created, str) else (created or {}).get("id")
        log(f"startup script uploaded -> {sid}")
        return sid
    except Exception as e:
        log(f"WARN startup script upload failed ({e}); booking without provisioning")
        return None


def existing_instances(v):
    """Instances that already exist and are not being torn down."""
    try:
        rows = v.get("/instances") or []
    except urllib.error.HTTPError as e:
        log(f"WARN could not list instances ({e.code}); refusing to book blind")
        raise
    dead = {"discontinued", "deleted", "terminated", "error"}
    return [r for r in rows
            if isinstance(r, dict) and str(r.get("status", "")).lower() not in dead]


def resolve(v):
    """Look up the image id and ssh key id by name, so nothing is hardcoded."""
    images = v.get("/images") or []
    image_id = None
    for img in images:
        name = f"{img.get('name', '')} {img.get('image_type', '')}".strip()
        if IMAGE_NAME.lower() in name.lower():
            image_id = img.get("id") or img.get("image_type")
            log(f"image: {name} -> {image_id}")
            break
    if not image_id:
        names = [i.get("name") for i in images][:20]
        raise SystemExit(f"No image matching {IMAGE_NAME!r}. Available: {names}")

    keys = v.get("/sshkeys") or []
    key_ids = [k.get("id") for k in keys
               if SSH_KEY_NAME.lower() in str(k.get("name", "")).lower()]
    if not key_ids:
        names = [k.get("name") for k in keys]
        raise SystemExit(f"No SSH key matching {SSH_KEY_NAME!r}. Available: {names}")
    log(f"ssh key: {SSH_KEY_NAME} -> {key_ids}")
    return image_id, key_ids


def book(v, image_id, key_ids, location, startup_id=None):
    payload = {
        "instance_type": INSTANCE_TYPE,
        "image": image_id,
        "ssh_key_ids": key_ids,
        "hostname": HOSTNAME,
        "description": f"{HOSTNAME} (auto-booked by verda-sniper)",
        "location_code": location,
        "os_volume": {"name": f"{HOSTNAME}-os", "size": OS_DISK_GB},
        "is_spot": False,
    }
    if startup_id:
        payload["startup_script_id"] = startup_id
    if DRY_RUN:
        log(f"DRY RUN — would POST /instances {json.dumps(payload)}")
        return {"dry_run": True, **payload}
    log(f"BOOKING {INSTANCE_TYPE} in {location} ...")
    return v.post("/instances", payload)


def describe(v, created):
    """Best-effort fetch of the new instance's IP for the notification."""
    try:
        for row in existing_instances(v):
            if row.get("hostname") == HOSTNAME or row.get("id") == created:
                return row
    except Exception:
        pass
    return {"id": created}


def probe(v):
    """Fire the real booking POST to prove the payload shape is accepted.

    The booking call had only ever been checked against the API spec, never
    actually sent. This sends it for real. If stock exists it books (which is
    the whole point). If not, Verda rejects it - and the *reason* is the proof:
    "not available" means our payload was understood and only stock was
    missing, while a 4xx naming a field means the payload is malformed and the
    watcher would have failed at the moment it mattered.
    """
    running = existing_instances(v)
    if running:
        log(f"instance already exists ({[r.get('hostname') for r in running]}); not probing")
        return 0
    image_id, key_ids = resolve(v)
    startup_id = ensure_startup_script(v)
    payload = {
        "instance_type": INSTANCE_TYPE,
        "image": image_id,
        "ssh_key_ids": key_ids,
        "hostname": HOSTNAME,
        "description": f"{HOSTNAME} (auto-booked by verda-sniper)",
        "location_code": os.environ.get("VERDA_PROBE_LOCATION", "FIN-02"),
        "os_volume": {"name": f"{HOSTNAME}-os", "size": OS_DISK_GB},
        "is_spot": False,
    }
    if startup_id:
        payload["startup_script_id"] = startup_id
    log(f"PROBE: POST /instances {json.dumps(payload)}")
    try:
        created = v.post("/instances", payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:600]
        log(f"PROBE RESULT {e.code}: {body}")
        low = body.lower()
        if "avail" in low or "capacity" in low or "stock" in low or "sold" in low:
            log("VERDICT: payload ACCEPTED - rejected only for lack of stock. "
                "The booking call is correct and will work when an A6000 frees up.")
            return 0
        log("VERDICT: payload REJECTED on its own merits - this is a real bug. "
            "The watcher would have failed at the moment it mattered.")
        return 4
    inst_id = created if isinstance(created, str) else (created or {}).get("id")
    info = describe(v, inst_id)
    result = {"booked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "instance_type": INSTANCE_TYPE, "id": inst_id,
              "hostname": info.get("hostname", HOSTNAME),
              "ip": info.get("ip") or info.get("public_ip") or "pending",
              "via": "probe"}
    log("PROBE BOOKED IT: " + json.dumps(result))
    with open("BOOKED.json", "w") as fh:
        json.dump(result, fh, indent=2)
    return 0


def main():
    cid = os.environ.get("VERDA_CLIENT_ID")
    secret = os.environ.get("VERDA_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("VERDA_CLIENT_ID / VERDA_CLIENT_SECRET are required")

    v = Verda(cid, secret)

    running = existing_instances(v)
    if running:
        log(f"An instance already exists ({[r.get('hostname') for r in running]}). "
            "Nothing to do — refusing to book a second one.")
        return 0

    if PROBE:
        return probe(v)

    alive, _ = check_feed_alive(v)
    if not alive:
        log("refusing to watch blind - fix the availability query first")
        return 3

    image_id, key_ids = resolve(v)
    startup_id = ensure_startup_script(v)
    log(f"watching for {INSTANCE_TYPE}, polling every {POLL_SECONDS}s "
        f"for {RUN_SECONDS}s")

    deadline = time.time() + RUN_SECONDS
    polls = 0
    while time.time() < deadline:
        polls += 1
        try:
            # No query params. `?is_spot=false` was silently returning nothing,
            # which is indistinguishable from "no stock" - it cost 32 hours of
            # blind polling while the console showed A6000 available.
            payload = v.get("/instance-availability")
            locs = locations_with(payload, INSTANCE_TYPE)
            # Log the available set whenever it changes, so a mismatch between
            # what the console shows and what we parse can never hide again.
            seen = all_types(payload)
            global _LAST_SEEN
            if seen != _LAST_SEEN:
                log(f"available now ({len(seen)}): {seen}")
                _LAST_SEEN = seen
            if not seen:
                # The feed went blind mid-run. Bail so the run fails visibly
                # rather than quietly reporting no stock for six hours.
                log("FEED DEAD mid-run: zero instance types reported")
                return 3
        except urllib.error.HTTPError as e:
            log(f"poll error {e.code}: {e.read().decode()[:200]}")
            time.sleep(POLL_SECONDS)
            continue
        except Exception as e:
            log(f"poll error: {e}")
            time.sleep(POLL_SECONDS)
            continue

        if locs:
            log(f"*** AVAILABLE in {locs} after {polls} polls")
            # Re-check right before spending: another run may have just booked.
            if existing_instances(v):
                log("another run booked it first — standing down")
                return 0
            try:
                created = book(v, image_id, key_ids, locs[0], startup_id)
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                log(f"BOOK FAILED {e.code}: {detail}")
                # Seeing stock and failing to take it is the expensive case, so
                # it must page the user rather than scroll past in a log.
                with open("BOOK_FAILED.json", "w") as fh:
                    json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "http_status": e.code, "detail": detail,
                               "location": locs[0]}, fh, indent=2)
                # Stock may have gone in the last second, or balance is too low.
                time.sleep(POLL_SECONDS)
                continue
            inst_id = created if isinstance(created, str) else created.get("id")
            info = describe(v, inst_id)
            result = {
                "booked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "instance_type": INSTANCE_TYPE,
                "location": locs[0],
                "id": inst_id,
                "hostname": info.get("hostname", HOSTNAME),
                "ip": info.get("ip") or info.get("public_ip") or "pending",
                "dry_run": DRY_RUN,
            }
            log("BOOKED: " + json.dumps(result))
            with open("BOOKED.json", "w") as fh:
                json.dump(result, fh, indent=2)
            return 0

        time.sleep(POLL_SECONDS)

    log(f"no availability in {polls} polls; exiting for the next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
