# Installing fraud-generator-helper as a systemd service

Assumes a Linux server (Debian/Ubuntu/RHEL-family) with systemd. If you're
targeting Windows, use NSSM or Task Scheduler instead — this guide doesn't
cover that.

## 1. Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg
```

`ffmpeg` also provides `ffprobe`; both must be on `PATH` for the user that
runs the service (verify with `ffmpeg -version` / `ffprobe -version`).

## 2. Create a dedicated service user

```bash
sudo useradd --system --create-home --home-dir /opt/fraud-generator-helper --shell /usr/sbin/nologin fraudgen
```

## 3. Deploy the code

```bash
sudo mkdir -p /opt/fraud-generator-helper
sudo rsync -a --exclude='.venv' --exclude='.git' ./ /opt/fraud-generator-helper/
sudo chown -R fraudgen:fraudgen /opt/fraud-generator-helper
```

(Swap `rsync` for `git clone` / `git pull` if you're deploying from a repo
directly on the server.)

## 4. Create the virtualenv and install dependencies

```bash
sudo -u fraudgen python3 -m venv /opt/fraud-generator-helper/.venv
sudo -u fraudgen /opt/fraud-generator-helper/.venv/bin/pip install -r /opt/fraud-generator-helper/requirements.txt
```

## 5. Configure

Edit `/opt/fraud-generator-helper/config.conf` for this environment (MQTT
broker host/port, S3 bucket, region, topic names, etc.). Leave
`aws.access_key_id` / `aws.secret_access_key` blank if the host has an IAM
instance/task role — otherwise reference them via `env:` as already set up
in the shipped config, e.g.:

```ini
[aws]
secret_access_key = env:AWS_SECRET_ACCESS_KEY
[mqtt]
password = env:MQTT_PASSWORD
```

Then create the actual secrets file (never committed to git):

```bash
sudo mkdir -p /etc/fraud-generator-helper
sudo cp deploy/fraud-generator-helper.env.example /etc/fraud-generator-helper/fraud-generator-helper.env
sudo $EDITOR /etc/fraud-generator-helper/fraud-generator-helper.env
sudo chown root:fraudgen /etc/fraud-generator-helper/fraud-generator-helper.env
sudo chmod 640 /etc/fraud-generator-helper/fraud-generator-helper.env
```

## 6. Runtime directories

`cache/`, `data/`, `work/`, and `logs/` are created automatically on first
run, but make sure the service user owns the project directory (step 3
already covers this via `chown -R`).

Note on the source cache: it holds exactly one copy of the source video at
a time (fixed filename, atomically replaced on refresh), so even at ~11GB
it won't accumulate multiple copies on disk — just make sure the volume
backing `/opt/fraud-generator-helper/cache` has at least ~2x the source
video's size free for the atomic swap during a refresh.

## 7. Install the unit file

```bash
sudo cp deploy/fraud-generator-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fraud-generator-helper
```

## 8. Verify

```bash
sudo systemctl status fraud-generator-helper
sudo journalctl -u fraud-generator-helper -f
tail -f /opt/fraud-generator-helper/logs/$(date +%F).log
```

The service should show `active (running)`, and you should see structured
JSON log lines in both `journalctl` (stdout is captured there too) and
today's file under `logs/`. That file name rolls over automatically at
midnight — no restart needed.

## 9. Common operations

```bash
# Restart after a config or code change
sudo systemctl restart fraud-generator-helper

# Stop
sudo systemctl stop fraud-generator-helper

# Disable from starting on boot
sudo systemctl disable fraud-generator-helper

# Tail today's structured logs
tail -f /opt/fraud-generator-helper/logs/$(date +%F).log
```

## 10. Updating a deployment

```bash
sudo systemctl stop fraud-generator-helper
sudo rsync -a --exclude='.venv' --exclude='.git' ./ /opt/fraud-generator-helper/
sudo -u fraudgen /opt/fraud-generator-helper/.venv/bin/pip install -r /opt/fraud-generator-helper/requirements.txt
sudo systemctl start fraud-generator-helper
```
