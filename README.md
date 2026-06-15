# Survey AB App

## File Structure

```
survey_app/
├── config.json
│
├── backend/
│   └── server.py
│
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   └── src/
│       ├── main.jsx
│       ├── index.css
│       ├── App.jsx
│       ├── App.css
│       └── components/
│           ├── Header.jsx
│           ├── Header.css
│           ├── SurveyCard.jsx
│           ├── SurveyCard.css
│           ├── ABGroup.jsx
│           ├── ABGroup.css
│           ├── SimpleQuestions.jsx
│           ├── SimpleQuestions.css
│           ├── QuestionBlock.jsx
│           └── QuestionBlock.css
│
├── questions/
│   ├── questions.csv
│   ├── languages.csv
│   ├── english.csv
│   └── (other language CSVs, e.g. german.csv)
│
├── scenarios.csv
│
├── <image_dir>/                     ← path set in config.json "images_path"
│   └── images.csv
│
└── user_data/                       ← auto-created on first answer
    ├── {scenario}_images.csv
    └── {scenario}_user_data.csv
```

---

## Local Development

You need two terminals running simultaneously: one for the backend, one for the frontend.

### Prerequisites

- Python 3.10+
- Node.js 18+
- `uv` (recommended) or `pip`

---

### 1. Install Python dependencies

**Option A — uv (recommended)**

`uv` manages its own virtual environment automatically. All commands must be run via `uv run`.

```bash
cd survey_app
uv add fastapi uvicorn pandas numpy
```

**Option B — pip**

```bash
cd survey_app
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install fastapi uvicorn pandas numpy
```

---

### 2. Start the backend

**Option A — uv**

```bash
cd survey_app
uv run uvicorn backend.server:app --reload --port 8000
```

**Option B — pip (with venv activated)**

```bash
cd survey_app
source .venv/bin/activate      # Windows: .venv\Scripts\activate
uv run uvicorn backend.server:app --reload --port 8000```

The API will be available at `http://localhost:8000`.  
`--reload` automatically restarts the server when you edit `server.py`.

---

### 3. Start the frontend

Open a second terminal:

```bash
cd survey_app/frontend
npm install       # only needed the first time
npm run dev
```

The app will be available at `http://localhost:5173`.

The Vite dev server proxies all `/api/*` and `/images/*` requests to `localhost:8000`,
so the frontend and backend work together without any CORS issues.

---

### 4. Verify it's working

- Backend health check: open `http://localhost:8000/docs` — you should see the FastAPI docs
- Frontend: open `http://localhost:5173` — the survey should load

---

## Production Deployment via SSH

### Overview

In production the React app is compiled into static files and served directly by FastAPI,
so only one process needs to run.

---

### Step 1 — Build the frontend

On your **local machine**, build the React app:

```bash
cd survey_app/frontend
npm run build
```

This creates `frontend/dist/` containing the compiled static files.

---

### Step 2 — Copy files to the server

Replace `user@your-server.com` and `/srv/survey_app` with your actual server address and target path.

```bash
# Copy the entire project (first time)
scp -r survey_app/ user@your-server.com:/srv/survey_app

# Or if you only updated the frontend build
scp -r survey_app/frontend/dist/ user@your-server.com:/srv/survey_app/frontend/dist
```

---

### Step 3 — Update server.py to serve the built frontend

At the bottom of `backend/server.py`, replace the development static file mount with:

```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

FRONTEND_DIST = os.path.join(ROOT, "frontend", "dist")

# Serve static assets (JS, CSS, images)
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

# Serve index.html for all non-API routes (SPA catch-all)
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
```

---

### Step 4 — Install dependencies on the server

SSH into your server:

```bash
ssh user@your-server.com
cd /srv/survey_app
```

**Option A — uv**

```bash
uv add fastapi uvicorn pandas numpy
```

**Option B — pip**

```bash
python -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn pandas numpy
```

---

### Step 5 — Run with a process manager

Use `systemd` to keep the server running and restart it on crashes.

```bash
sudo nano /etc/systemd/system/survey.service
```

**Option A — uv**

```ini
[Unit]
Description=Survey AB App
After=network.target

[Service]
User=your-linux-username
WorkingDirectory=/srv/survey_app
ExecStart=/home/your-linux-username/.local/bin/uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Option B — pip (venv)**

```ini
[Unit]
Description=Survey AB App
After=network.target

[Service]
User=your-linux-username
WorkingDirectory=/srv/survey_app
ExecStart=/srv/survey_app/.venv/bin/uvicorn backend.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable survey
sudo systemctl start survey
```

Check that it's running:

```bash
sudo systemctl status survey
```

---

### Step 6 — (Optional) Put Nginx in front

If you want to serve on port 80/443 or add HTTPS, install Nginx:

```bash
sudo apt install nginx
```

Create a site config:

```bash
sudo nano /etc/nginx/sites-available/survey
```

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/survey /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

For HTTPS, use Certbot:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

---

### Updating the app after changes

```bash
# On your local machine — rebuild frontend
cd survey_app/frontend
npm run build

# Copy updated files to server
scp -r frontend/dist/ user@your-server.com:/srv/survey_app/frontend/dist
scp backend/server.py user@your-server.com:/srv/survey_app/backend/server.py

# On the server — restart the service
ssh user@your-server.com "sudo systemctl restart survey"

```

---

### Future improvements 

user_id is now assigned randomly and could be repeated (very unlikely) 
n-digits in question options is not being forced 

Local MongoDB 
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod

sudo systemctl stop mongod

# create the new data directory
mkdir -p /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb

# copy existing data (if any was already written) to the new location
sudo rsync -av /var/lib/mongodb/ /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb/

# fix ownership - mongod runs as user 'mongodb'
sudo chown -R mongodb:mongodb /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb

sudo nano /etc/mongod.conf

storage:
  dbPath: /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb

or

docker run -d --name mongo -p 27017:27017 mongo:7


py


utils.py
313 lines

py


trueskill_utils.py
277 lines

py


server.py
387 lines

py


db.py
48 lines

py


index.html
13 lines

html


README.md
339 lines

md

Help deplaying this app  in my server. I need to host the db in the same server and in the user_data folder. Here's the complete setup from zero to running. Run these commands on your server over SSH.
1. Install dependencies
bash

# System packages
sudo apt update && sudo apt install -y python3-pip python3-venv git curl

# MongoDB
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
Upload your project bash
# From your local machine
rsync -av --exclude='user_data/' --exclude='__pycache__/' --exclude='.git/' \
  /path/to/survey_app/ user@yourserver:/home/user/survey_app/
Install Python deps on the server bash
cd /home/user/survey_app
pip install uv
uv add motor
uv sync
Run it bash
export MONGODB_URI="mongodb://localhost:27017"
export MONGODB_DB="survey_app"
uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
Test it works, then set it up as a persistent service:
5. Create a systemd service so it survives reboots and restarts on crash:
bash

sudo nano /etc/systemd/system/survey.service
Paste this (adjust paths and user):
ini

[Unit]
Description=Survey App
After=network.target mongod.service
Requires=mongod.service

[Service]
User=user
WorkingDirectory=/home/user/survey_app
Environment="MONGODB_URI=mongodb://localhost:27017"
Environment="MONGODB_DB=survey_app"
ExecStart=/home/user/.local/bin/uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
Then:
bash

sudo systemctl daemon-reload
sudo systemctl enable survey
sudo systemctl start survey
sudo systemctl status survey   # should show Active: running
Optional — expose on port 80 with nginx (so users don't need :8000): bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/survey
nginx

server {
    listen 80;
    server_name yourserver.com;  # or _ for any

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
bash

sudo ln -s /etc/nginx/sites-available/survey /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
After that your app is at http://yourserver.com and restarts automatically on reboot or crash.

Show more
12:36 AM
Your plan is solid overall. A few fixes/notes:

1. MongoDB data location — by default mongod stores data in /var/lib/mongodb, not in your user_data folder. If you want the DB data inside your project's user_data/, configure /etc/mongod.conf:

yaml
storage:
  dbPath: /home/user/survey_app/user_data/mongodb
Then:

bash
sudo mkdir -p /home/user/survey_app/user_data/mongodb
sudo chown -R mongodb:mongodb /home/user/survey_app/user_data/mongodb
sudo systemctl restart mongod
Otherwise just leave the default path — it's fine, and arguably safer (no risk of accidentally rsync --delete-ing your DB).

2. uv add motor requires a pyproject.toml — your repo doesn't show one. If it doesn't exist, run uv init first (or uv add will create a minimal one, but check it picks up fastapi/uvicorn/pandas/numpy/trueskill too — those aren't in your README's uv add list either). Make sure all deps end up in pyproject.toml:

bash
cd /home/user/survey_app
uv add fastapi uvicorn pandas numpy motor trueskill
3. frontend/dist must exist — server.py (per your README's step 3) serves frontend/dist/index.html via SPA fallback. Your rsync excludes nothing frontend-related, but make sure you've run npm run build locally first and that frontend/dist/ is actually included in the rsync (it's not git-tracked usually, so double check it's not implicitly excluded).

4. Static image mounts — server.py mounts directories from CONFIG["images_path"] at startup. Make sure those CSVs/image folders are present on the server at the same relative paths as in config.json, or update config.json for the server's layout.

5. Firewall — if using nginx on port 80, also:

bash
sudo ufw allow 80/tcp
sudo ufw allow OpenSSH
and consider not exposing port 8000 externally (sudo ufw deny 8000/tcp or just don't open it).

6. systemd uv run path — verify with which uv as that user; it's sometimes /home/user/.cargo/bin/uv instead of .local/bin/uv. Fix the ExecStart path accordingly or systemd will fail silently with "command not found."

Everything else (mongod service ordering, systemd unit, nginx reverse proxy) looks correct.

Last login: Fri Jun 12 16:33:12 2026 from 94.134.178.174
$ ls -l
total 832
drwxrwxr-x 3 miguelup miguelup   4096 Mar  4 04:05 __MACOSX
drwxr-xr-x 2 miguelup miguelup   4096 Mar  4 04:08 dist
-rw-r--r-- 1 miguelup miguelup 842464 Mar  4 04:04 dist.zip
$ 
 download the existing files you see on this server so you can learn how it was deployed i nthe past

12:37 AM
I don't have SSH access to your server or any ability to connect to it — I can't run commands there or download files from it. I can only work with files you upload here, or run commands in my own sandboxed container.

If you want me to inspect how it was deployed, you'd need to either:

Paste the relevant file contents here (e.g. cat dist/index.html, any *.service files, config.json, nginx configs, etc.)
Run commands yourself on the server and share the output
A few useful things to check and paste back:

bash
ls -la /home/miguelup/
ls -la /home/miguelup/dist/
unzip -l dist.zip | head -30
sudo systemctl list-units | grep -i survey
sudo find / -maxdepth 6 -iname "*survey*" 2>/dev/null
sudo find / -maxdepth 6 -iname "config.json" 2>/dev/null
cat /etc/nginx/sites-enabled/* 2>/dev/null
sudo systemctl status mongod
That'll tell us whether there's an existing survey_app directory elsewhere, what the systemd service is named, and how nginx is wired up — then I can help adapt it.

There is not survey app but there is another app

12:38 AM
Got it — what does that other app's setup look like? Paste the output of those commands (or whichever you ran) and I'll take a look — particularly the systemd service file, nginx config, and directory structure, since those will tell us how to slot the survey app in alongside it (port choice, nginx routing, service naming, etc.).

It is similar. I need the setup of my app to be similar

12:38 AM
Sure — share the relevant details from the existing app and I'll mirror the pattern for the survey app. Specifically useful:

Directory layout — ls -la of wherever it lives (e.g. /home/miguelup/<app>/ or /srv/...)
systemd service file — cat /etc/systemd/system/<appname>.service
nginx config — cat /etc/nginx/sites-enabled/<appname>
How Python deps are managed — is there a pyproject.toml/.venv, or is it using uv, or plain pip?
Mongo setup — is it using a separate Mongo DB name, same instance? cat whatever sets MONGODB_URI/MONGODB_DB in its service file or .env
Port it runs on, and whether nginx proxies / or a subpath
Once I see these I'll write the matching systemd unit + nginx block + deployment steps for the survey app so it follows the same conventions (same user, same uv/venv style, same nginx server-block pattern, distinct port and Mongo DB name to avoid collisions).


._api
_api


._assets
_assets


._dist
_dist


._index-ojNSlLDY.js
1 line

js


._index-DZLsy2DN.js
1 line

js


._index-CFJAqkNs.css
1 line

css


._save-validation-data.js
1 line

js


._index.html
1 line

html

This is the existing app there. It is in __MACOSX

12:41 AM
These uploads are just macOS resource-fork metadata files (the ._* AppleDouble files from a zip extracted on Mac) — they don't contain the actual code, just Finder attribute junk. The real files (index.html, the JS bundles, save-validation-data.js, __api, __assets, __dist) weren't actually uploaded — only their __MACOSX/._* shadow copies were.

This tells us something useful though: that other app is a built frontend (Vite/React dist) + a separate api folder, similar structure to what you're deploying.

To actually see the setup, on the server run:

bash
ls -la /home/miguelup/
ls -la /home/miguelup/dist/
find / -maxdepth 6 -iname "*.service" 2>/dev/null | xargs -I{} sh -c 'echo "--- {} ---"; cat {}' 2>/dev/null
ls /etc/nginx/sites-enabled/
cat /etc/nginx/sites-enabled/*
sudo find / -maxdepth 8 -path "*/api/*.py" 2>/dev/null
sudo find / -maxdepth 8 -iname "config.json" 2>/dev/null
Paste the output (especially the systemd unit and nginx config) and I'll mirror that exact pattern for the survey app — same conventions for paths, ports, service naming, and Mongo usage.


miguel@aguacate:~$ ssh miguelup@cs-futurecities.media.mit.edu miguelup@cs-futurecities.media.mit.edu's password: Linux cs-futurecities 6.12.48+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.48-1 (2025-09-20) x86_64 The programs included with the Debian GNU/Linux system are free software; the exact

pasted

12:42 AM
Now I have the picture. Key findings:

No survey-style app is actually deployed yet — only nginx's default site (port 80, serving /var/www/html, no proxy_pass to any app).
No systemd service for any Python/FastAPI app exists.
No MongoDB installed (no mongod service found).
The "other app" (with dist/, dist.zip, __api, etc.) is just sitting unextracted in miguelup's home dir — it was never deployed either, just uploaded.
miguelup doesn't have passwordless sudo (the sudo prompt failed).
So there's nothing to mirror — it's a fresh box. Given that, here's the deployment plan for your survey app, adapted to what we now know:

1. Get sudo working. You need the actual sudo password for miguelup, or ask the server admin (this is cs-futurecities.media.mit.edu, likely MIT Media Lab managed — you may not have full sudo at all). Without sudo you can't install MongoDB, edit nginx, or create systemd services.

2. If you can't get sudo: ask the admin to install mongodb-org and nginx config changes for you, or run everything as an unprivileged user:

MongoDB: not installable without root. Alternative — run MongoDB via a user-level container (if Docker/Podman available) or ask admin.
Run uvicorn on an unprivileged port (e.g. 8001) under your own user via systemd --user (no sudo needed for user units!).
Ask admin to add an nginx location /survey/ { proxy_pass http://127.0.0.1:8001/; } block to the existing default site, or a new server block.
3. Upload your survey app:

bash
rsync -av --exclude='user_data/' --exclude='__pycache__/' \
  /path/to/survey_app/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/
4. Use a systemd user service (no sudo needed):

bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/survey.service
ini
[Unit]
Description=Survey App
After=network.target

[Service]
WorkingDirectory=/home/miguelup/survey_app
Environment="MONGODB_URI=mongodb://localhost:27017"
Environment="MONGODB_DB=survey_app"
ExecStart=/home/miguelup/.local/bin/uv run uvicorn backend.server:app --host 127.0.0.1 --port 8001
Restart=always

[Install]
WantedBy=default.target
bash
systemctl --user daemon-reload
systemctl --user enable --now survey.service
loginctl enable-linger miguelup   # keeps it running after logout
Next step: can you get the sudo password, or do you need to ask the sysadmin to install MongoDB and add an nginx location block? That determines which path we take.

$ rsync -av --exclude='user_data/' --exclude='__pycache__/' \
  /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/> 
-sh: 10: rsync: not found
$

12:43 AM
rsync isn't installed on either your local machine or possibly the remote — but the error -sh: 10: rsync: not found with sh suggests it's missing locally (your machine), since that's where you're running the command.

Quick fixes, in order of preference:

1. Install rsync locally

bash
sudo apt install rsync      # Debian/Ubuntu
brew install rsync          # macOS
2. If you can't install anything, use scp instead (slower, no exclude support, so clean up first):

bash
cd /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app
rm -rf user_data __pycache__ backend/__pycache__ frontend/node_modules
scp -r . miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/
3. Or tar it up and pipe over ssh (works without rsync, supports excludes):

bash
cd /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app
tar --exclude='user_data' --exclude='__pycache__' --exclude='node_modules' \
    -czf - . | ssh miguelup@cs-futurecities.media.mit.edu \
    'mkdir -p /home/miguelup/survey_app && tar -xzf - -C /home/miguelup/survey_app'
Option 3 is best if you can't install rsync — single command, no temp files, respects excludes.

ocale: Cannot set LC_ALL to default locale: No such file or directory
Selecting previously unselected package rsync.
(Reading database ... 34513 files and directories currently installed.)
Preparing to unpack .../rsync_3.4.1+ds1-5+deb13u3_amd64.deb ...
Unpacking rsync (3.4.1+ds1-5+deb13u3) ...
Setting up rsync (3.4.1+ds1-5+deb13u3) ...
rsync.service is a disabled or a static unit, not starting it.
Processing triggers for man-db (2.13.1-1) ...
$ rsync -av --exclude='user_data/' --exclude='__pycache__/' \
  /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/> 
The authenticity of host 'cs-futurecities.media.mit.edu (127.0.1.1)' can't be established.
ED25519 key fingerprint is SHA256:xYxlzNAZFFNbMs9dVqnkAiCZZbZEr0in1c+JtIYXy1M.
This key is not known by any other names.
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
Warning: Permanently added 'cs-futurecities.media.mit.edu' (ED25519) to the list of known hosts.
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
rsync: [sender] change_dir "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr" failed: No such file or directory (2)
created directory /home/miguelup/survey_app
sent 19 bytes  received 60 bytes  3.67 bytes/sec
total size is 0  speedup is 0.00
rsync error: some files/attrs were not transferred (see previous errors) (code 23) at main.c(1347) [sender=3.4.1]
$ rsync -av --exclude='user_data/' --exclude='__pycache__/' \
>   /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
rsync: [sender] change_dir "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr" failed: No such file or directory (2)
sent 19 bytes  received 12 bytes  3.65 bytes/sec
total size is 0  speedup is 0.00
rsync error: some files/attrs were not transferred (see previous errors) (code 23) at main.c(1347) [sender=3.4.1]

Show more
12:47 AM
The path /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app doesn't exist on your local machine — that's why rsync's sender side fails ("change_dir ... failed: No such file or directory").

Check the actual path:

bash
ls ~/Documents/UNI/Master/
find ~ -maxdepth 6 -iname "survey_app" -type d 2>/dev/null
Likely culprits: wrong capitalization/spacing in a folder name (e.g. "Projekt Verkehr" with a space, or "2." vs "2"), or it's actually under a different path entirely (Desktop, Downloads, etc.).

Once you find the real path, also note: your rsync command ends the source with survey_app (no trailing slash) and destination with survey_app/ — that will create /home/miguelup/survey_app/survey_app/... (nested). To copy the contents of survey_app directly into /home/miguelup/survey_app/, add a trailing slash to the source:

bash
rsync -av --exclude='user_data/' --exclude='__pycache__/' \
  /correct/path/to/survey_app/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/
(trailing slash on source = copy contents; no trailing slash = copy the folder itself into destination)

Where should I execute the command on my local or rmeote?

12:48 AM
On your local machine (the one with your project files). rsync's source path needs to point to files on the machine where you run the command — the destination (miguelup@...:/path) is the remote side it pushes to.

So: open a terminal on your laptop/desktop where survey_app actually lives, cd there or use the full local path, and run the rsync command from there — not from the SSH session into the MIT server.

I did upload the files to the remote

12:53 AM
Good — now let's verify the upload landed correctly and continue with deployment. On the remote (SSH session), check:

bash
ls -la /home/miguelup/survey_app/
You should see config.json, backend/, frontend/, questions/, scenarios.csv, etc. (per the README's structure).

A few things to check/do next:

1. Confirm frontend build exists

bash
ls /home/miguelup/survey_app/frontend/dist/
If empty/missing, you need to build it locally first (npm run build in frontend/) and rsync frontend/dist/ over too — the production server.py (per the README step 3 edit) serves from there.

2. Check __pycache__/.git weren't accidentally included and that user_data/ is empty or absent (clean start).

3. Now, regarding sudo/MongoDB — earlier your sudo password prompt failed ("Sorry, try again"). Do you have the correct sudo password for miguelup on this server, or do you need to request MongoDB installation from the sysadmin? This blocks steps 1 (MongoDB install) and the nginx/systemd setup from the original plan.

Let me know the output of the ls commands above and whether you have sudo access, and we'll proceed.

$ ls -la /home/miguelup/survey_app/
total 12
drwxrwxr-x 3 miguelup miguelup 4096 Jun 13 18:48 .
drwxr-xr-x 6 miguelup root     4096 Jun 13 18:46 ..
drwxrwxr-x 7 miguelup miguelup 4096 Jun 13 18:27 survey_app

12:54 AM
There's the nesting issue — since your local rsync source didn't have a trailing slash, the survey_app folder got copied inside the destination survey_app folder.

Fix it by moving contents up one level:

bash
cd /home/miguelup/survey_app
mv survey_app/* survey_app/.[!.]* . 2>/dev/null
rmdir survey_app
ls -la
(The .[!.]* glob catches hidden files like .gitignore if any; ignore errors if none exist.)

Then verify:

bash
ls -la /home/miguelup/survey_app/
You should now see config.json, backend/, frontend/, etc. directly under /home/miguelup/survey_app/.

No frontend build. You have to tell me how to do that

12:54 AM
Building the frontend needs to happen wherever Node.js is available — could be your local machine or the remote server, whichever has Node installed.

Check the remote first (simplest if it works):

bash
node -v
npm -v
If Node is available on the remote, build there:

bash
cd /home/miguelup/survey_app/frontend
npm install
npm run build
This creates frontend/dist/. Done — no need to upload anything extra.

If Node is NOT on the remote (likely no sudo to install it), build locally:

bash
# On your local machine, in the project folder
cd /path/to/survey_app/frontend
npm install
npm run build
This creates frontend/dist/ locally. Then upload just that folder:

bash
rsync -av /path/to/survey_app/frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
If you don't have Node locally either, install it first:

macOS: brew install node
Debian/Ubuntu: sudo apt install nodejs npm (or use nvm for a recent version — Debian's repo version may be too old for Vite; check with node -v, you want 18+)
Run node -v on both machines and tell me what you get, and I'll point you to the right one.

^C(survey-app) miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ uv run uvicorn backend.server:app --reload --port 8000
INFO:     Will watch for changes in these directories: ['/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app']
Traceback (most recent call last):
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/bin/uvicorn", line 10, in <module>
    sys.exit(main())
             ^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/click/core.py", line 1524, in __call__
    return self.main(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/click/core.py", line 1445, in main
    rv = self.invoke(ctx)
         ^^^^^^^^^^^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/click/core.py", line 1308, in invoke
    return ctx.invoke(self.callback, **ctx.params)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/click/core.py", line 877, in invoke
    return callback(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/uvicorn/main.py", line 441, in main
    run(
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/uvicorn/main.py", line 609, in run
    config.load_app()
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/uvicorn/config.py", line 415, in load_app
    return import_from_string(self.app)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/uvicorn/importer.py", line 22, in import_from_string
    raise exc from None
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/.venv/lib/python3.12/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1331, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 935, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 995, in exec_module
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/backend/server.py", line 45, in <module>
    from . import db as _db
  File "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/backend/db.py", line 24, in <module>
    from motor.motor_asyncio import AsyncIOMotorClient
ModuleNotFoundError: No module named 'motor'

Show more
12:56 AM
This is on your local machine. motor isn't installed in the venv. Fix:

bash
cd /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app
uv add motor
Then re-run:

bash
uv run uvicorn backend.server:app --reload --port 8000
If uv add motor doesn't pick up the existing .venv correctly, you can also do:

bash
uv pip install motor
Note: you'll also need MongoDB running locally for this to fully work (the lifespan startup calls _db.image_state_col() and queries it). If you don't have MongoDB locally, either install it (sudo apt install -y mongodb-org per the earlier instructions, or docker run -d -p 27017:27017 mongo), or just test this on the remote where you're setting it up for real.


(survey-app) miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ uv add motor Resolved 31 packages in 265ms Prepared 3 packages in 246ms Installed 3 packages in 11ms + dnspython==2.8.0 + motor==3.7.1 + pymongo==4.17.0 (survey-app) miguel@aguacate:~/Documents/UNI/Master/2/ProjektVe

pasted

(survey-app) miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/frontend$ npm run dev
> survey-frontend@0.0.0 dev
> vite
  VITE v8.0.16  ready in 156 ms
  ➜  Local:   http://localhost:5173/
  ➜  Network: use --host to expose
  ➜  press h + enter to show help
12:56:39 AM [vite] http proxy error: /api/languages
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)
12:56:39 AM [vite] http proxy error: /api/new-user
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)
12:56:39 AM [vite] http proxy error: /api/survey?language=english
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)
12:56:49 AM [vite] http proxy error: /api/languages
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)
12:56:49 AM [vite] http proxy error: /api/new-user
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)
12:56:49 AM [vite] http proxy error: /api/survey?language=english
Error: connect ECONNREFUSED 127.0.0.1:8000
    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1634:16)

Show more
12:57 AM
Both errors trace back to one thing: MongoDB isn't running locally. The backend won't start (so port 8000 is closed → vite proxy errors).

Install and start MongoDB locally:

bash
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
If lsb_release -cs returns a codename MongoDB doesn't support yet (e.g. very new Ubuntu/Debian), substitute a known-supported codename like jammy in that line.

Alternative if you don't want to install MongoDB system-wide, use Docker:

bash
docker run -d --name mongo -p 27017:27017 mongo:7
Once mongod is running, restart the backend:

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
uv run uvicorn backend.server:app --reload --port 8000
It should now start cleanly, mount images, and /api/* calls from the Vite dev server will work.

miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
[sudo] password for miguel: 
sudo: a password is required
[sudo] password for miguel:                                                                                         
13Sorry, try again.
[sudo] password for miguel:        
deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu zara/mongodb-org/7.0 multiverse
Get:1 file:/var/cuda-repo-ubuntu2204-12-6-local  InRelease [1.572 B]
Get:2 file:/var/cuda-repo-ubuntu2404-12-6-local  InRelease [1.572 B]
Get:1 file:/var/cuda-repo-ubuntu2204-12-6-local  InRelease [1.572 B]
Get:2 file:/var/cuda-repo-ubuntu2404-12-6-local  InRelease [1.572 B]
Hit:3 https://download.docker.com/linux/ubuntu noble InRelease                                                      
Hit:4 http://dl.google.com/linux/earth/deb stable InRelease                                                         
Hit:5 http://archive.ubuntu.com/ubuntu noble InRelease                                                              
Hit:6 http://security.ubuntu.com/ubuntu noble-security InRelease                                                    
Hit:7 http://archive.ubuntu.com/ubuntu noble-updates InRelease                                                      
Hit:8 https://repo.protonvpn.com/debian stable InRelease                                                            
Hit:9 https://packages.microsoft.com/repos/code stable InRelease                                                    
Hit:10 http://archive.ubuntu.com/ubuntu noble-backports InRelease                                                   
Hit:11 https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble InRelease                                       
Ign:13 https://repo.mongodb.org/apt/ubuntu zara/mongodb-org/7.0 InRelease                                           
Ign:14 http://packages.linuxmint.com zara InRelease                                       
Hit:12 https://nbg1.your-objectstorage.com/qgis-download/debian noble InRelease           
Hit:15 http://packages.linuxmint.com zara Release                   
Err:16 https://repo.mongodb.org/apt/ubuntu zara/mongodb-org/7.0 Release
  404  Not Found [IP: 2600:9000:2449:d400:0:bd83:86c0:93a1 443]
Get:17 https://zotero.retorque.re/file/apt-package-archive ./ InRelease [1.781 B]
Reading package lists... Done
N: Skipping acquire of configured file 'stable/binary-i386/Packages' as repository 'https://download.docker.com/linux/ubuntu noble InRelease' doesn't support architecture 'i386'
E: The repository 'https://repo.mongodb.org/apt/ubuntu zara/mongodb-org/7.0 Release' does not have a Release file.
N: Updating from such a repository can't be done securely, and is therefore disabled by default.
N: See apt-secure(8) manpage for repository creation and user configuration details.
Failed to start mongod.service: Unit mongod.service not found.
Failed to enable unit: Unit file mongod.service does not exist.
Unit mongod.service could not be found.

Show more
12:58 AM
Two issues:

You're on Linux Mint (codename zara), which MongoDB's repo doesn't recognize. Use the Ubuntu base codename instead — Mint 22 (zara) is based on Ubuntu 24.04 (noble).
The gpg key import likely failed too since sudo failed the first time — re-run everything cleanly:
bash
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list

sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
If noble also 404s for MongoDB 7.0 (sometimes the repo lags behind for the very newest Ubuntu), fall back to jammy:

bash
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
Also remove the bad list file first to avoid leftover errors:

bash
sudo rm -f /etc/apt/sources.list.d/mongodb-org-7.0.list
then redo the echo ... | sudo tee step with the correct codename.

Setting up mongodb-database-tools (100.17.0) ...
Setting up mongodb-org-mongos (7.0.37) ...
Setting up mongodb-org-database-tools-extra (7.0.37) ...
Setting up mongodb-org-database (7.0.37) ...
Setting up mongodb-org-tools (7.0.37) ...
Setting up mongodb-org (7.0.37) ...
Processing triggers for man-db (2.12.0-4build2) ...
miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
Created symlink /etc/systemd/system/multi-user.target.wants/mongod.service → /usr/lib/systemd/system/mongod.service.
● mongod.service - MongoDB Database Server
     Loaded: loaded (/usr/lib/systemd/system/mongod.service; enabled; preset: enabled)
     Active: active (running) since Sun 2026-06-14 01:00:47 CEST; 340ms ago
       Docs: https://docs.mongodb.org/manual
   Main PID: 82254 (mongod)
     Memory: 66.3M (peak: 67.7M)
        CPU: 146ms
     CGroup: /system.slice/mongod.service
             └─82254 /usr/bin/mongod --config /etc/mongod.conf
Jun 14 01:00:47 aguacate systemd[1]: Started mongod.service - MongoDB Database Server.
Jun 14 01:00:47 aguacate mongod[82254]: {"t":{"$date":"2026-06-13T23:00:47.275Z"},"s":"I",  "c":"CONTROL",  "id":748>
miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ docker run -d --name mongo -p 27017:27017 mongo:7
failed to connect to the docker API at unix:///home/miguel/.docker/desktop/docker.sock; check if the path is correct and if the daemon is running: dial unix /home/miguel/.docker/desktop/docker.sock: connect: no such file or directory
miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$

Show more
1:01 AM
MongoDB is running successfully via systemd — the Docker error is irrelevant now since you already have native MongoDB on port 27017. Ignore the docker command.

Now start the backend:

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
uv run uvicorn backend.server:app --reload --port 8000
It should start cleanly now and connect to MongoDB. Then in the other terminal, the Vite dev server's proxy errors should resolve — refresh http://localhost:5173.

where is the databse? I do not see it

1:02 AM
MongoDB stores its data files in /var/lib/mongodb by default (configured in /etc/mongod.conf). You won't see a single "database file" — it's a directory of internal storage files (WiredTiger .wt files), not human-readable.

To actually look at the data, use the Mongo shell or a GUI tool, not the filesystem:

bash
mongosh
Then inside the shell:

javascript
show dbs
use survey_app
show collections
db.image_state.find().limit(5).pretty()
db.user_answers.find().limit(5).pretty()
If survey_app doesn't appear in show dbs, it means the backend hasn't successfully written to it yet — Mongo only creates the database on first write/insert (lazy creation). Check that the backend started without errors and that a scenario was seeded (look for the [server] scenario=... images=... log line on startup).

If you specifically want the raw files location, check:

bash
cat /etc/mongod.conf | grep -A2 storage
ls -la /var/lib/mongodb/
And regarding your earlier requirement — wanting the DB data inside user_data/ — that's a separate config change (dbPath in mongod.conf) we discussed earlier; by default it's in /var/lib/mongodb, not in your project folder.

I need the data to be stored in survey_app/user_data

1:03 AM
Stop mongod, change the dbPath, migrate existing data, restart.

bash
sudo systemctl stop mongod

# create the new data directory
mkdir -p /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb

# copy existing data (if any was already written) to the new location
sudo rsync -av /var/lib/mongodb/ /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb/

# fix ownership - mongod runs as user 'mongodb'
sudo chown -R mongodb:mongodb /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb
Edit /etc/mongod.conf:

bash
sudo nano /etc/mongod.conf
Change the storage.dbPath line:

yaml
storage:
  dbPath: /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb
Important caveat with AppArmor on Ubuntu/Mint: the mongod AppArmor profile restricts which paths it can write to. A path under /home/... will likely be denied. Check:

bash
sudo systemctl start mongod
sudo systemctl status mongod
journalctl -u mongod -n 50 --no-pager
If it fails with permission/AppArmor errors, either:

Disable/adjust the AppArmor profile:
bash
sudo aa-complain /usr/sbin/mongod
or edit /etc/apparmor.d/usr.sbin.mongod to add an owner /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb/** rwk, rule, then sudo systemctl reload apparmor.

Or — simpler — use a bind mount so /var/lib/mongodb (which AppArmor already allows) points at your project folder:
bash
sudo mount --bind /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb /var/lib/mongodb
and add it to /etc/fstab for persistence:

/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0
This keeps dbPath unchanged in mongod.conf (no AppArmor edits needed) while the data physically lives in your project's user_data/.

I'd recommend the bind-mount approach — fewer AppArmor headaches.

When I deploy the app to my remote how will it work?

1:04 AM
Same approach, but two extra considerations on the remote (cs-futurecities.media.mit.edu):

1. You need sudo there too — for installing MongoDB, editing /etc/mongod.conf or AppArmor, and setting up the bind mount. Earlier your sudo password failed on that server. If you genuinely don't have sudo on the remote, you can't use this approach there at all — you'd need to ask the sysadmin, or run MongoDB in a way that doesn't need root (e.g. a user-namespace container, or just point dbPath to wherever sudo does let you write).

2. The path will differ — your remote project lives at /home/miguelup/survey_app/, not the local path. Adjust accordingly:

bash
sudo systemctl stop mongod
mkdir -p /home/miguelup/survey_app/user_data/mongodb
sudo rsync -av /var/lib/mongodb/ /home/miguelup/survey_app/user_data/mongodb/
sudo chown -R mongodb:mongodb /home/miguelup/survey_app/user_data/mongodb

sudo mount --bind /home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb
Add to /etc/fstab:

/home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0
Everything else (installing mongodb-org, MONGODB_URI=mongodb://localhost:27017, MONGODB_DB=survey_app, the systemd service for the app) stays the same as what we discussed before.

Practical alternative: if sudo/AppArmor on the remote turns out to be too restrictive, it's perfectly fine to just leave MongoDB's data in the default /var/lib/mongodb on the remote — the requirement to store it in user_data/ matters mainly for your local dev convenience (so you can inspect/backup easily). On a shared university server, default system paths with proper systemd management are arguably more robust anyway. Worth weighing whether this constraint is essential for the deployed copy.

Do you know if you have working sudo on the remote yet?

First tell me how to build the app

1:05 AM
Building the frontend (do this wherever Node is available — locally or on remote, whichever has Node 18+):

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/frontend
npm install
npm run build
This produces frontend/dist/. Verify:

bash
ls dist/
If building locally, then upload dist/ to the remote:

bash
rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
After that, per the README's production step, backend/server.py needs the SPA-serving block added at the bottom (replacing/alongside the dev static mount) — let me know once the build is done and I'll help edit server.py for that.

miguel@aguacate:~$ rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
rsync: [sender] change_dir "/home/miguel/frontend/dist" failed: No such file or directory (2)
rsync: [Receiver] mkdir "/home/miguelup/survey_app/frontend/dist" failed: No such file or directory (2)
rsync error: error in file IO (code 11) at main.c(800) [Receiver=3.4.1]
miguel@aguacate:~$

1:06 AM
You're in ~ (home directory), not the project folder. frontend/dist doesn't exist relative to ~.

Use the full path:

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
Also note the receiver error — mkdir .../frontend/dist failed: No such file or directory. This likely means /home/miguelup/survey_app/frontend/ doesn't exist on the remote either (rsync can create the final dist dir but not multiple missing parent dirs in older versions). Check on remote:

bash
ls /home/miguelup/survey_app/frontend/
If frontend/ itself is missing, create it first:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'mkdir -p /home/miguelup/survey_app/frontend'
then re-run the rsync from the correct local directory.

miguel@aguacate:~$ cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
rsync: [Receiver] mkdir "/home/miguelup/survey_app/frontend/dist" failed: No such file or directory (2)
rsync error: error in file IO (code 11) at main.c(800) [Receiver=3.4.1]
miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ The app is here: /home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app

1:07 AM
Good, the local path is now correct (sending file list worked). The remaining issue is on the remote: /home/miguelup/survey_app/frontend/ doesn't exist yet.

Create it first:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'mkdir -p /home/miguelup/survey_app/frontend'
Then re-run:

bash
rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
This should now succeed. If frontend/ is also missing other things you'd expect (since the README structure shows frontend/src/, package.json etc. should already be there from your earlier upload), check:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'ls -la /home/miguelup/survey_app/'
to confirm what actually made it over in the initial rsync — earlier we found a nesting issue (survey_app/survey_app/) that you fixed manually, so it's worth double-checking the full tree is intact.

miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ rsync -av --delete frontend/dist/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/frontend/dist/
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
created directory /home/miguelup/survey_app/frontend/dist
./
index.html
assets/
assets/index-BZ9krMeE.js
assets/index-CRTX6Jex.css
sent 209.694 bytes  received 150 bytes  27.979,20 bytes/sec
total size is 209.329  speedup is 1,00
Tell me the next steps

Show more
1:08 AM
Frontend is uploaded. Now:

1. Edit backend/server.py to serve the built frontend instead of (or alongside) the dev mount. Per the README's production instructions, replace the bottom static-mount section.

Let's view the current end of server.py on the remote and edit it. First, let's confirm the current state:


Placeholder
Placeholder
I don't have direct access to your remote — you'll run these. Here's the plan:

1. SSH in and edit server.py:

bash
ssh miguelup@cs-futurecities.media.mit.edu
nano /home/miguelup/survey_app/backend/server.py
Find this block at the bottom (the static mounts section):

python
for _idx, _csv_path in enumerate(_to_path_list(CONFIG["images_path"])):
    _parent = os.path.dirname(os.path.abspath(_csv_path))
    _route  = f"/images/{_idx}"
    app.mount(_route, StaticFiles(directory=_parent), name=f"images_{_idx}")
    print(f"[server] Mounted {_parent!r}  →  {_route}")

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(ROOT, "frontend")),
    name="static",
)
Replace the last block (app.mount("/static", ...)) with:

python
FRONTEND_DIST = os.path.join(ROOT, "frontend", "dist")

app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

from fastapi.responses import FileResponse

@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
Also remove/check the existing @app.get("/") index route near the top — it currently does:

python
@app.get("/")
def index() -> HTMLResponse:
    with open(os.path.join(ROOT, "frontend", "index.html"), "r") as f:
        return HTMLResponse(f.read())
This points to frontend/index.html (the dev template), not frontend/dist/index.html. Either remove this route entirely (the new catch-all spa_fallback will handle /) or fix the path to frontend/dist/index.html. I'd remove it — the catch-all covers it.

2. Set up MongoDB on the remote (install steps from earlier — need sudo working there).

3. Set up the systemd service for the backend (as discussed, either system-wide if sudo works, or systemd --user if not).

4. nginx — proxy port 80 to the app's port, or if you have sudo, edit the existing nginx default site.

Let me know once you've made the server.py edit and confirm sudo status on the remote — that determines steps 2-4.

What shoulñd I do here? @app.get("/")
def index() -> HTMLResponse:
    with open(os.path.join(ROOT, "frontend", "index.html"), "r") as f:
        return HTMLResponse(f.read())

1:10 AM
Delete that whole function. The new catch-all spa_fallback route will handle / (and every other non-API path) by serving frontend/dist/index.html, which is the actual built app — the old route was pointing at the dev-only frontend/index.html template that doesn't reference the built JS/CSS bundles.

Also remove the now-unused HTMLResponse import if it's not used elsewhere in the file:

python
from fastapi.responses import HTMLResponse
(check with a quick search — if HTMLResponse isn't referenced anywhere else, delete that import line too.)

In config these paths are relative to survey_apo. Will it work? 

{
    "scenario":"Anlagenring",
    "user_data_path":"user_data",
    "scenarios_path":"scenarios.csv",
    "images_path":"images/Anlagenring/images.csv",
    "languages_path":"questions/languages.csv",
    "questions_path":"questions/questions.csv",
    "batch_size": 20,
    "uncertainty_threshold": 0.4,
    "active_learning": 0.8
}
Show more
1:11 AM
Yes — these paths work as-is, as long as the directory structure exists relative to survey_app/ (the ROOT).

In utils.py:

python
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
__file__ is backend/utils.py, so ROOT = survey_app/. Same in server.py. All _resolve_paths/_to_path_list calls join relative paths onto ROOT.

So on the remote, this config requires:

/home/miguelup/survey_app/scenarios.csv
/home/miguelup/survey_app/images/Anlagenring/images.csv
/home/miguelup/survey_app/questions/languages.csv
/home/miguelup/survey_app/questions/questions.csv
/home/miguelup/survey_app/user_data/   (auto-created if missing)
Verify these exist on the remote:

bash
ls /home/miguelup/survey_app/scenarios.csv
ls /home/miguelup/survey_app/images/Anlagenring/images.csv
ls /home/miguelup/survey_app/questions/
If any are missing, your initial rsync didn't bring them over (possibly excluded, or they live outside the survey_app folder locally — e.g. the images/ folder can sometimes be huge and stored elsewhere). Check locally too:

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
ls scenarios.csv images/Anlagenring/images.csv questions/
If they exist locally but not remotely, sync them over (the images folder especially — make sure it wasn't excluded and isn't too large to have transferred).

upload config.csv and the new sever.py to the remote

1:11 AM
Run these from your local project directory (~/Documents/UNI/Master/2/ProjektVerkehr/survey_app):

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app

# upload config.json
rsync -av config.json miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/config.json

# upload updated server.py
rsync -av backend/server.py miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/backend/server.py
Note: you wrote config.csv but the file is config.json (per your earlier paste) — adjust if you actually meant a different file.

If you edited server.py directly on the remote via nano (as I suggested earlier), you don't need to upload it — it's already there. Only run the server.py rsync if you edited it locally and the remote copy is stale.

miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
# upload config.json
rsync -av config.json miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/config.json
# upload updated server.py
rsync -av backend/server.py miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/backend/server.py
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
config.json
sent 449 bytes  received 35 bytes  74,46 bytes/sec
total size is 335  speedup is 0,69
miguelup@cs-futurecities.media.mit.edu's password: 
sending incremental file list
rsync: [Receiver] change_dir#3 "/home/miguelup/survey_app/backend" failed: No such file or directory (2)
rsync error: errors selecting input/output files, dirs (code 3) at main.c(838) [Receiver=3.4.1]

Show more
1:12 AM
config.json uploaded fine. The backend/ directory doesn't exist on the remote — meaning your initial project upload didn't include it (or got lost in the nesting fix earlier).

Check what's actually on the remote:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'ls -la /home/miguelup/survey_app/'
If backend/ is missing, upload the whole backend folder:

bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
rsync -av backend/ miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/backend/
This will create the directory and sync all files (server.py, db.py, utils.py, trueskill_utils.py, pairing.py, __init__.py if present, etc.). Then verify:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'ls -la /home/miguelup/survey_app/backend/'
I see the issue is that there are two  nestes survey_app/survey_app move the second one to hte first

1:13 AM
On the remote, fix the nesting:

bash
ssh miguelup@cs-futurecities.media.mit.edu
Then:

bash
cd /home/miguelup/survey_app
ls -la
Confirm there's a nested survey_app/ inside. Then move its contents up:

bash
cd /home/miguelup/survey_app
mv survey_app/* .
mv survey_app/.[!.]* . 2>/dev/null
rmdir survey_app
If mv survey_app/* . complains about existing files/directories (e.g. frontend/ exists in both and mv won't merge directories), use rsync instead to merge:

bash
rsync -av survey_app/ .
rm -rf survey_app
This merges the nested folder's contents into the parent without clobbering files you've already placed correctly (like frontend/dist/ and config.json).

Then verify:

bash
ls -la /home/miguelup/survey_app/
You should see backend/, frontend/, config.json, scenarios.csv, questions/, images/, etc. all at the top level.

I uploaded all files. Next steps? Make shure mongodb database is in suyrvey_app/user_data

1:16 AM
Good. Now let's set up MongoDB on the remote with data in survey_app/user_data.

1. Check sudo access on the remote first:

bash
ssh miguelup@cs-futurecities.media.mit.edu
sudo -v
If this fails (as it did earlier), you can't install/configure MongoDB system-wide and we need a different approach (ask sysadmin, or a rootless container). Let me know the result.

Assuming sudo works, install MongoDB:

bash
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
(Remember: the server is Debian 13, not Ubuntu — noble may 404 like before. If it 404s, try jammy. Worst case, fall back to MongoDB 6.0's repo which has broader Debian support, or check what worked on your local Mint box and use the same codename.)

2. Once mongod is running, move data into survey_app/user_data via bind mount (avoids AppArmor issues with /home paths):

bash
sudo systemctl stop mongod

mkdir -p /home/miguelup/survey_app/user_data/mongodb

sudo rsync -av /var/lib/mongodb/ /home/miguelup/survey_app/user_data/mongodb/

sudo chown -R mongodb:mongodb /home/miguelup/survey_app/user_data/mongodb

sudo mount --bind /home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb

sudo systemctl start mongod
sudo systemctl status mongod
Make it persistent across reboots — add to /etc/fstab:

bash
echo "/home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0" | sudo tee -a /etc/fstab
3. Verify:

bash
mongosh --eval "db.adminCommand('ping')"
ls /var/lib/mongodb/
ls /home/miguelup/survey_app/user_data/mongodb/
Both directory listings should show the same files (since it's a bind mount).

Run sudo -v first and tell me whether it works before proceeding.

The remote seems to not remember past commands and not let me copy

1:17 AM
That's just a normal SSH terminal limitation — bash history and clipboard don't carry over automatically, and that's expected, not a bug.

A few practical tips:

For history: each ssh session starts fresh in terms of displayed history navigation, but commands you ran in a previous session are usually still in ~/.bash_history on the remote — press the Up arrow to cycle through them once you're connected, or run:

bash
history | tail -30
For copy/paste: terminal copy/paste depends on your local terminal emulator, not SSH. If you're using a basic terminal, select text with your mouse and use middle-click or Ctrl+Shift+C/V (Linux terminals) or Cmd+C/V (Mac/iTerm). If you're in a TUI like nano or tmux, mouse selection might be captured by the app — try holding Shift while selecting to bypass it.

Easier approach overall: rather than typing long commands manually in the SSH session, write them as a script locally, then upload and run it:

bash
# locally, create setup_mongo.sh with all the commands
nano setup_mongo.sh
paste the mongo install + bind-mount commands into it, save, then:

bash
chmod +x setup_mongo.sh
rsync -av setup_mongo.sh miguelup@cs-futurecities.media.mit.edu:~/
ssh miguelup@cs-futurecities.media.mit.edu 'bash ~/setup_mongo.sh'
This avoids retyping/copy-paste issues entirely. Want me to write that script for you now?

[sudo] password for miguelup: 
$ history | tail -30
-sh: 2: history: not found
$

1:17 AM
That -sh: prompt and history: not found suggest you're in a minimal shell (sh/dash), not bash — likely because the SSH session is using a restricted or non-interactive shell, or sh is your login shell.

Check your shell:

bash
echo $SHELL
ps -p $$
If it's dash/sh, switch to bash for a better experience:

bash
bash
Then history should work (assuming bash is installed — it should be on Debian).

But honestly, let's not fight the shell. Let's go with the script approach — write the setup commands locally as a .sh file, upload it, and run it with bash script.sh regardless of what your default login shell is. Want me to write the MongoDB setup script now? First confirm: did sudo -v succeed (does sudo work on the remote)?

miguel@aguacate:~/Documents/UNI/Master/2/ProjektVerkehr/survey_app$ ssh miguelup@cs-futurecities.media.mit.edu
sudo -v
miguelup@cs-futurecities.media.mit.edu's password: 
Linux cs-futurecities 6.12.48+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.48-1 (2025-09-20) x86_64
The programs included with the Debian GNU/Linux system are free software;
the exact distribution terms for each program are described in the
individual files in /usr/share/doc/*/copyright.
Debian GNU/Linux comes with ABSOLUTELY NO WARRANTY, to the extent
permitted by applicable law.
Last login: Sat Jun 13 18:35:11 2026 from 94.134.178.170
$ sudo -v
[sudo] password for miguelup: 
$ history | tail -30
-sh: 2: history: not found
$ bash
miguelup@cs-futurecities:~$ curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update && sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
bash: curl: command not found
sudo: gpg: command not found
deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 multiverse
Hit:1 http://security.debian.org/debian-security trixie-security InRelease
Hit:2 http://ftp.us.debian.org/debian trixie InRelease
Hit:3 http://ftp.us.debian.org/debian trixie-updates InRelease
Ign:4 https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 InRelease
Err:5 https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 Release
  404  Not Found [IP: 18.239.168.8 443]
Error: The repository 'https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/7.0 Release' does not have a Release file.
Notice: Updating from such a repository can't be done securely, and is therefore disabled by default.
Notice: See apt-secure(8) manpage for repository creation and user configuration details.
Failed to start mongod.service: Unit mongod.service not found.
Failed to enable unit: Unit mongod.service does not exist
Unit mongod.service could not be found.
miguelup@cs-futurecities:~$

Show more
1:18 AM
Good news: sudo works. Issues to fix:

curl/gpg missing — install them first.
Debian Trixie — MongoDB's official repo doesn't support Trixie yet. Use the Debian 12 (bookworm) repo, which works fine on Trixie for MongoDB packages.
Run this:

bash
sudo apt update
sudo apt install -y curl gnupg

curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list

sudo apt update
sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
If mongodb-org install fails due to glibc/library version mismatches between bookworm packages and trixie (sometimes happens), fall back to MongoDB 8.0 which has better Debian 12/13 support:

bash
echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update
sudo apt install -y mongodb-org
Run the first block and paste the output.


e /etc/apt/sources.list.d/mongodb-org-7.0.list sudo apt update sudo apt install -y mongodb-org sudo systemctl start mongod sudo systemctl enable mongod sudo systemctl status mongod Hit:1 http://security.debian.org/debian-security trixie-security InRelease Hit:2 http://ftp.us.debian.org/debian trixi

pasted

1:19 AM
Trixie's newer sqv/sequoia-based signature verification rejects MongoDB 7.0's GPG key because it uses SHA1, which is now considered insecure. Switch to MongoDB 8.0, which uses a modern key:

bash
sudo rm -f /etc/apt/sources.list.d/mongodb-org-7.0.list /usr/share/keyrings/mongodb-server-7.0.gpg

curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | sudo gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list

sudo apt update
sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod
sudo systemctl status mongod
If MongoDB 8.0 also fails the same signature check (unlikely, but the new key might still use SHA1 for an older self-signature), as a last resort you can downgrade the policy:

bash
echo 'Apt::Key::Sha1AllowedSignatures "true";' | sudo tee /etc/apt/apt.conf.d/99mongo-sha1
then retry sudo apt update. This is a narrow workaround just for this repo's key — not ideal long-term, but functional. Try 8.0 first.


sudo apt update sudo apt install -y mongodb-org sudo systemctl start mongod sudo systemctl enable mongod sudo systemctl status mongod deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main Hit:1 http://security.

pasted

1:20 AM
MongoDB 8.0 is installed and running. Now let's move the data dir into survey_app/user_data.

Press q to exit that status pager view first, then run:

bash
sudo systemctl stop mongod

mkdir -p /home/miguelup/survey_app/user_data/mongodb

sudo rsync -av /var/lib/mongodb/ /home/miguelup/survey_app/user_data/mongodb/

sudo chown -R mongodb:mongodb /home/miguelup/survey_app/user_data/mongodb

sudo mount --bind /home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb

sudo systemctl start mongod
sudo systemctl status mongod
Make it persistent across reboots:

bash
echo "/home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0" | sudo tee -a /etc/fstab
Verify:

bash
mongosh --eval "db.adminCommand('ping')"
ls /var/lib/mongodb/
ls /home/miguelup/survey_app/user_data/mongodb/
Both listings should show the same WiredTiger files. Once confirmed, we move on to: installing Python deps (uv, motor, etc.), setting up the systemd service for the FastAPI app, and nginx.

tting up mongodb-org-mongos (8.0.26) ...
Setting up mongodb-org-database-tools-extra (8.0.26) ...
Setting up mongodb-org-database (8.0.26) ...
Setting up mongodb-org-tools (8.0.26) ...
Setting up mongodb-org (8.0.26) ...
Processing triggers for man-db (2.13.1-1) ...
Created symlink '/etc/systemd/system/multi-user.target.wants/mongod.service' → '/usr/lib/systemd/system/mongod.service'.
● mongod.service - MongoDB Database Server
     Loaded: loaded (/usr/lib/systemd/system/mongod.service; enabled; preset: e>
     Active: active (running) since Sat 2026-06-13 19:19:45 EDT; 535ms ago
 Invocation: fa4558f4ac4f4e4f8ab2266e8e541d12
       Docs: https://docs.mongodb.org/manual
   Main PID: 1670572 (mongod)
     Memory: 1.3M (peak: 2.9M)
        CPU: 11ms
     CGroup: /system.slice/mongod.service
             └─1670572 "[mongod]"
Jun 13 19:19:45 cs-futurecities systemd[1]: Started mongod.service - MongoDB Da>
Jun 13 19:19:48 cs-futurecities mongod[1670572]: {"t":{"$date":"2026-06-13T23:1>
miguelup@cs-futurecities:~$ sudo systemctl stop mongod
mkdir -p /home/miguelup/survey_app/user_data/mongodb
sudo rsync -av /var/lib/mongodb/ /home/miguelup/survey_app/user_data/mongodb/
sudo chown -R mongodb:mongodb /home/miguelup/survey_app/user_data/mongodb
sudo mount --bind /home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb
sudo systemctl start mongod
sudo systemctl status mongod
sending incremental file list
./
WiredTiger
WiredTiger.lock
WiredTiger.turtle
WiredTiger.wt
WiredTigerHS.wt
_mdb_catalog.wt
collection-0-2613639495810337603.wt
collection-2-2613639495810337603.wt
collection-4-2613639495810337603.wt
index-1-2613639495810337603.wt
index-3-2613639495810337603.wt
index-5-2613639495810337603.wt
index-6-2613639495810337603.wt
mongod.lock
sizeStorer.wt
storage.bson
diagnostic.data/
diagnostic.data/metrics.2026-06-13T23-19-49Z-00000
journal/
journal/WiredTigerLog.0000000001
journal/WiredTigerPreplog.0000000001
sent 210,020,720 bytes  received 400 bytes  140,014,080.00 bytes/sec
total size is 209,967,895  speedup is 1.00
● mongod.service - MongoDB Database Server
     Loaded: loaded (/usr/lib/systemd/system/mongod.service; enabled; preset: e>
     Active: active (running) since Sat 2026-06-13 19:20:25 EDT; 42ms ago
 Invocation: 819fb2dd3c1747ac9ff3a0fb1bfd3c01
       Docs: https://docs.mongodb.org/manual
   Main PID: 1670690 (mongod)
     Memory: 2M (peak: 3.1M)
        CPU: 8ms
     CGroup: /system.slice/mongod.service
             └─1670690 /usr/bin/mongod --config /etc/mongod.conf
Jun 13 19:20:25 cs-futurecities systemd[1]: Started mongod.service - MongoDB Da>
miguelup@cs-futurecities:~$ echo "/home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0" | sudo tee -a /etc/fstab
/home/miguelup/survey_app/user_data/mongodb /var/lib/mongodb none bind 0 0
miguelup@cs-futurecities:~$ mongosh --eval "db.adminCommand('ping')"
ls /var/lib/mongodb/
ls /home/miguelup/survey_app/user_data/mongodb/
{ ok: 1 }
WiredTiger                 diagnostic.data
WiredTiger.lock                 index-1-2613639495810337603.wt
WiredTiger.turtle             index-3-2613639495810337603.wt
WiredTiger.wt                 index-5-2613639495810337603.wt
WiredTigerHS.wt                 index-6-2613639495810337603.wt
_mdb_catalog.wt                 journal
collection-0-2613639495810337603.wt  mongod.lock
collection-2-2613639495810337603.wt  sizeStorer.wt
collection-4-2613639495810337603.wt  storage.bson
WiredTiger                 diagnostic.data
WiredTiger.lock                 index-1-2613639495810337603.wt
WiredTiger.turtle             index-3-2613639495810337603.wt
WiredTiger.wt                 index-5-2613639495810337603.wt
WiredTigerHS.wt                 index-6-2613639495810337603.wt
_mdb_catalog.wt                 journal
collection-0-2613639495810337603.wt  mongod.lock
collection-2-2613639495810337603.wt  sizeStorer.wt
collection-4-2613639495810337603.wt  storage.bson
miguelup@cs-futurecities:~$

Show more
1:20 AM
MongoDB is running, bind-mounted into survey_app/user_data/mongodb, and persisted via fstab.

Now let's set up the Python app. Check for uv and Python deps:

bash
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
which uv
Then install dependencies and test the app:

bash
cd /home/miguelup/survey_app
uv sync 2>/dev/null || uv add fastapi uvicorn pandas numpy motor trueskill
(if there's no pyproject.toml yet, uv add will create one and a .venv)

Then test run it:

bash
export MONGODB_URI="mongodb://localhost:27017"
export MONGODB_DB="survey_app"
uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
Watch for errors — particularly check the [server] Mounted ... and [server] scenario=... log lines to confirm images_path and CSVs resolve correctly (per our earlier discussion about config.json paths). Then Ctrl+C and let me know the output, and we'll set up the systemd service + nginx next.

miguelup@cs-futurecities:~$ which uv || curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
which uv
downloading uv 0.11.21 x86_64-unknown-linux-gnu
installing to /home/miguelup/.local/bin
  uv
  uvx
everything's installed!
To add $HOME/.local/bin to your PATH, either restart your shell or run:
    source $HOME/.local/bin/env (sh, bash, zsh)
    source $HOME/.local/bin/env.fish (fish)
bash: /home/miguelup/.bashrc: No such file or directory
miguelup@cs-futurecities:~$

Show more
1:21 AM
uv is installed but not on PATH yet. Run:

bash
source $HOME/.local/bin/env
which uv
Then proceed:

bash
cd /home/miguelup/survey_app
uv add fastapi uvicorn pandas numpy motor trueskill
Then test:

bash
export MONGODB_URI="mongodb://localhost:27017"
export MONGODB_DB="survey_app"
uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
Watch the startup logs for the [server] Mounted ... and [server] scenario=... lines, then Ctrl+C and paste the output.

miguelup@cs-futurecities:~/survey_app$ export MONGODB_URI="mongodb://localhost:27017"
export MONGODB_DB="survey_app"
uv run uvicorn backend.server:app --host 0.0.0.0 --port 8000
/home/miguelup/survey_app/.venv/lib/python3.12/site-packages/trueskill/__init__.py:315: SyntaxWarning: invalid escape sequence '\ '
  | \ / |
[server] Mounted '/home/miguelup/survey_app/images/Anlagenring'  →  /images/0
INFO:     Started server process [1670936]
INFO:     Waiting for application startup.
[server] scenario='Anlagenring'  img_types=['start', 'walk', 'bike', 'stay']  images=222  sample _serve_path=/images/0/1.jpg
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)

Show more
1:21 AM
Working perfectly. Backend is up, MongoDB connected, images mounted, scenario loaded (222 images). The SyntaxWarning from trueskill is harmless, ignore it.

Ctrl+C to stop it, then let's set up the persistent service.

1. Create the systemd service:

bash
sudo nano /etc/systemd/system/survey.service
Paste:

ini
[Unit]
Description=Survey App
After=network.target mongod.service
Requires=mongod.service

[Service]
User=miguelup
WorkingDirectory=/home/miguelup/survey_app
Environment="MONGODB_URI=mongodb://localhost:27017"
Environment="MONGODB_DB=survey_app"
Environment="PATH=/home/miguelup/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/home/miguelup/.local/bin/uv run uvicorn backend.server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
(Using 127.0.0.1 instead of 0.0.0.0 since nginx will proxy — no need to expose 8000 externally.)

2. Enable and start:

bash
sudo systemctl daemon-reload
sudo systemctl enable survey
sudo systemctl start survey
sudo systemctl status survey
3. Check it's responding:

bash
curl -s http://127.0.0.1:8000/api/languages
Run these and paste the output, then we'll do nginx.

Tasks: 3 (limit: 4641)
     Memory: 11.7M (peak: 11.7M)
        CPU: 18ms
     CGroup: /system.slice/survey.service
             └─1671019 /home/miguelup/.local/bin/uv run uvicorn backend.server:>
Jun 13 19:22:38 cs-futurecities systemd[1]: Started survey.service - Survey App.
Jun 13 19:22:39 cs-futurecities uv[1671027]: INFO:     Started server process [>
Jun 13 19:22:39 cs-futurecities uv[1671027]: INFO:     Waiting for application >
Jun 13 19:22:39 cs-futurecities uv[1671027]: INFO:     Application startup comp>
Jun 13 19:22:39 cs-futurecities uv[1671027]: INFO:     Uvicorn running on http:>
miguelup@cs-futurecities:~/survey_app$ curl -s http://127.0.0.1:8000/api/languages
[{"language":"english","symbol":"🇬🇧 EN","file":"english.csv"},{"language":"german","symbol":"🇩🇪 DE","file":"german.csv"}]miguelup@cs-futurecities:~/survey_app$

Show more
1:22 AM
Service is running and responding correctly. Now nginx.

The previous nginx config we saw was just the default _ server block on port 80, serving /var/www/html. We'll either edit that default site or add a new server block. Since this server may host other things, let's add a dedicated location block to the existing default site (simplest, no DNS/server_name complexity needed for an IP/hostname-based setup).

1. Edit the default nginx site:

bash
sudo nano /etc/nginx/sites-available/default
Inside the existing server { listen 80 default_server; ... } block, add a proxy location before the existing location / { try_files ... } block (or replace it entirely if this server is dedicated to the survey app):

Option A — survey app at the root (replace the whole location / {...} block):

nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
Option B — survey app under /survey/ (keep existing / for other things, add a new location):

nginx
location /survey/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
Note: Option B is trickier because the FastAPI app's static asset paths (/assets/..., /images/..., /api/...) are absolute, not prefixed with /survey/ — they'd break unless you also add separate proxy rules for /assets/, /images/, /api/ or run the app behind a path-aware reverse proxy setup. Option A is much simpler if this server can dedicate port 80 to the survey app.

Which do you want — is this server shared with other apps on port 80, or can the survey app take over /?

This is the survey_app path on the remote /home/miguelup/survey_app

1:23 AM
Good, that path is confirmed correct and matches everything we've configured (systemd WorkingDirectory, bind mount, etc.) — no changes needed there.

Now back to the nginx decision: is this server (cs-futurecities.media.mit.edu) shared with other websites/apps on port 80, or can the survey app take over the root /? That determines whether we go with Option A (simple, root) or Option B (subpath, more complex).

take over root

1:23 AM
Good, Option A.

bash
sudo nano /etc/nginx/sites-available/default
Replace the existing location / { try_files $uri $uri/ =404; } block with:

nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
You can leave the rest of the server block (root /var/www/html;, index ...;, server_name _;, the commented PHP/.htaccess sections) as-is — they're harmless and unused once location / proxies everything.

Then test and reload:

bash
sudo nginx -t
sudo systemctl reload nginx
Then test from outside:

bash
curl -sI http://cs-futurecities.media.mit.edu/
curl -s http://cs-futurecities.media.mit.edu/api/languages
The first should return HTTP/1.1 200 OK with the SPA's index.html, and the second should return the same JSON we got via curl on 8000. Paste the results.

miguel@aguacate:~$ curl -sI http://cs-futurecities.media.mit.edu/
curl -s http://cs-futurecities.media.mit.edu/api/languages
HTTP/1.1 405 Method Not Allowed
Server: nginx
Date: Sat, 13 Jun 2026 23:25:05 GMT
Content-Type: application/json
Content-Length: 31
Connection: keep-alive
allow: GET
[{"language":"english","symbol":"🇬🇧 EN","file":"english.csv"},{"language":"german","symbol":"🇩🇪 DE","file":"german.csv"}]miguel@aguacate:~$

1:25 AM
Both are actually working — the proxy is functioning correctly.

The 405 Method Not Allowed on curl -sI / is because curl -I sends a HEAD request, and your spa_fallback catch-all route is defined with @app.get("/{full_path:path}") — FastAPI's @app.get doesn't automatically handle HEAD for path-parameter catch-alls the way it does for simple GET routes in some setups. This is a minor cosmetic issue; browsers use GET, not HEAD, so the site will load fine.

The /api/languages call returned the correct JSON — proxying works.

Verify the actual page loads with a normal GET:

bash
curl -s http://cs-futurecities.media.mit.edu/ | head -20
You should see the built index.html (referencing /assets/index-...js and .css). Then open http://cs-futurecities.media.mit.edu/ in a browser to confirm the survey UI renders and a pair of images loads.

If you want to fix the HEAD 405 (optional, cosmetic only), change:

python
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
to:

python
@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
def spa_fallback(full_path: str):
Not urgent — only matters if some tool/monitor does HEAD requests against /.

n","symbol":"🇩🇪 DE","file":"german.csv"}]miguel@aguacate:~$ curl -s http://cs-futurecities.media.micurl -s http://cs-futurecities.media.mit.edu/ | head -20
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Survey AB</title>
    <script type="module" crossorigin src="/assets/index-BZ9krMeE.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-CRTX6Jex.css">
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>
miguel@aguacate:~$

Show more
1:25 AM
That's the correct built index.html, referencing the production JS/CSS bundles. Everything is wired up correctly:

MongoDB running, data in survey_app/user_data/mongodb (bind-mounted, persists across reboots)
FastAPI backend running via systemd (survey.service), auto-restarts, starts on boot
nginx proxying port 80 → backend on 127.0.0.1:8000
Frontend build served, API working, images mounted
Open http://cs-futurecities.media.mit.edu/ in a browser and confirm the survey loads and you can answer a comparison — that's the real end-to-end test.

A couple of optional follow-ups worth considering:

HTTPS: if you want https://, you'd need a domain cert via certbot — but cs-futurecities.media.mit.edu may already have one managed centrally; check with the sysadmin before adding your own.
Logs: journalctl -u survey -f to tail backend logs if something looks off in the browser.
Redeploying after future code changes: rebuild frontend locally → rsync frontend/dist/ → rsync changed backend files → sudo systemctl restart survey.
Try it in the browser and let me know how it looks.

My browser says not secure

1:26 AM
That's expected and not a problem — your site is running over plain HTTP (http://), not HTTPS, so browsers show "Not Secure." The app itself works fine; it's just an unencrypted connection warning, common for internal/dev/research tools.

Whether to fix this depends on what the survey is used for:

If this is just for testing/internal use, you can ignore the warning — functionality isn't affected.
If real participants will use this (especially anything sensitive, or just for professionalism), you'll want HTTPS via a TLS certificate.
To add HTTPS with Let's Encrypt (free, auto-renewing):

bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d cs-futurecities.media.mit.edu
This requires:

The domain cs-futurecities.media.mit.edu must resolve (via DNS) to this server's public IP — likely true since you're SSHing to it by that name.
Port 443 must be reachable from the internet (check firewall/MIT network policies).
Since this is an MIT-managed subdomain, there may already be a TLS cert managed centrally, or restrictions on running your own certbot — worth checking with Media Lab IT before running this, as it could conflict with existing cert management.
If certbot succeeds, it auto-edits the nginx config to add the listen 443 ssl block and redirect HTTP→HTTPS, and https://cs-futurecities.media.mit.edu/ will work with a valid cert.

Want to try it, or check with IT first?

How can I see the user data images mongodb data?

1:27 AM
Use mongosh to inspect it directly:

bash
mongosh survey_app
Then inside the shell:

javascript
// list collections
show collections

// see image state documents (TrueSkill scores, etc.)
db.image_state.find().limit(5).pretty()
db.image_state.countDocuments()

// see submitted answers
db.user_answers.find().limit(5).pretty()
db.user_answers.countDocuments()

// example: top-scoring images for a question
db.image_state.find({}, {img_id: 1, score_<qid>: 1}).sort({score_<qid>: -1}).limit(10)
(replace <qid> with an actual question id from your questions.csv)

Export to CSV/JSON for analysis (e.g. in pandas):

bash
mongoexport --db=survey_app --collection=user_answers --type=csv --fields=user_id,scenario,language,question_id,type,answer,img_id_A,img_id_B,img_type,info --out=/home/miguelup/survey_app/user_data/user_answers.csv

mongoexport --db=survey_app --collection=image_state --type=json --out=/home/miguelup/survey_app/user_data/image_state.json
Then download to your local machine:

bash
rsync -av miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/user_data/user_answers.csv ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/
Note: since /var/lib/mongodb is bind-mounted to survey_app/user_data/mongodb, the raw WiredTiger files are technically "in" that folder, but they're binary database storage files — not human-readable. mongoexport is the way to get usable CSV/JSON out.

where did the file donwload?

1:29 AM
The rsync command downloads to wherever you run it locally — specifically into:

~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/
So the file would be at:

/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/user_answers.csv
Check it:

bash
ls -la ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/
If it's not there, the mongoexport step on the remote may not have actually run yet (did you run that command first, on the remote, before the rsync?). Confirm on the remote:

bash
ssh miguelup@cs-futurecities.media.mit.edu 'ls -la /home/miguelup/survey_app/user_data/'
If user_answers.csv exists there, the rsync should pull it down. If the rsync command itself errored, paste the output.

user_id,scenario,language,question_id,type,answer,img_id_A,img_id_B,img_type,info
9550186,Anlagenring,english,start,AB,A,t1,t2,start,
9550186,Anlagenring,english,walk-init,CHOICE,Continue,,,,
9550186,Anlagenring,english,walk-preference,AB,B,w62,w49,walk,
9550186,Anlagenring,english,walk-preference,AB,A,w75,w59,walk,
9550186,Anlagenring,english,walk-preference,AB,A,w49,w3,walk,
9550186,Anlagenring,english,walk-preference,AB,B,w52,w75,walk,
9550186,Anlagenring,english,walk-preference,AB,B,w61,w59,walk,
9550186,Anlagenring,english,stay-init,CHOICE,Continue,,,,
9550186,Anlagenring,english,stay-preference,AB,A,s20,s19,stay,
9550186,Anlagenring,english,stay-preference,AB,B,s33,s47,stay,
9550186,Anlagenring,english,stay-preference,AB,A,s20,s16,stay,
9550186,Anlagenring,english,stay-preference,AB,B,s19,s32,stay,
9550186,Anlagenring,english,stay-preference,AB,B,s44,s47,stay, I do not want this. I want the other dataset with the trueskill scores for the images and the batch_ids and so
Show more
1:30 AM
To get the TrueSkill scores / batch_ids dataset, export image_state instead. On the remote:

bash
mongoexport --db=survey_app --collection=image_state --type=csv --fields=img_id,scenario,img_type,batch_id,path,incompatible_ids --out=/home/miguelup/survey_app/user_data/image_state_base.csv
But that only gets static fields — the score/uncertainty/n_answers/active_batch columns are dynamic (score_<qid>, uncertainty_<qid>, etc. for each question id), so --fields won't capture them all cleanly with mongoexport's CSV mode. Better to export as JSON and convert, or use mongoexport with --jsonArray and pandas:

bash
mongoexport --db=survey_app --collection=image_state --type=json --jsonArray --out=/home/miguelup/survey_app/user_data/image_state.json
Then download and convert locally:

bash
rsync -av miguelup@cs-futurecities.media.mit.edu:/home/miguelup/survey_app/user_data/image_state.json ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app/user_data/
bash
cd ~/Documents/UNI/Master/2/ProjektVerkehr/survey_app
python3 -c "
import pandas as pd
df = pd.read_json('user_data/image_state.json')
df.to_csv('user_data/image_state.csv', index=False)
print(df.columns.tolist())
print(df.head())
"
This gives you a full CSV with img_id, scenario, img_type, batch_id, score_<qid>, uncertainty_<qid>, n_answers_<qid>, active_batch_<qid> for every question id in your SAVE_QIDS.

delete aLL USER data from remote

1:32 AM
Before deleting, confirm exactly what you want gone — this is irreversible. Three options:

A. Wipe MongoDB data only (TrueSkill scores, user answers — keeps images/config/code):

bash
mongosh survey_app --eval "db.image_state.drop(); db.user_answers.drop()"
This drops the collections; they'll be recreated empty next time the app restarts (image_state gets reseeded with defaults from images.csv).

B. Wipe the entire survey_app database:

bash
mongosh --eval "db.getSiblingDB('survey_app').dropDatabase()"
C. Wipe everything in user_data/ (exported CSVs/JSON files too, plus the Mongo data dir — but be careful, that's the bind-mounted Mongo storage):

bash
sudo systemctl stop survey
sudo systemctl stop mongod
sudo rm -rf /home/miguelup/survey_app/user_data/mongodb/*
sudo rm -f /home/miguelup/survey_app/user_data/*.csv /home/miguelup/survey_app/user_data/*.json
sudo systemctl start mongod
sudo systemctl start survey
I'd recommend Option A — clears all collected survey responses and TrueSkill state, lets the app reseed fresh defaults on next request, keeps MongoDB itself intact (no service restarts needed).

Which option do you want? Confirm and I'll give the exact final commands.

I want the user data that where saved to go away so there are no existing answers as they where test

1:32 AM
That's option A — drop the test data, keep everything else running.

bash
mongosh survey_app --eval "db.image_state.drop(); db.user_answers.drop()"
Then restart the app so it reseeds image_state with fresh default TrueSkill values from images.csv:

bash
sudo systemctl restart survey
sudo systemctl status survey
Check the logs to confirm it reseeded cleanly:

bash
journalctl -u survey -n 20 --no-pager
You should see the same [server] scenario='Anlagenring' img_types=[...] images=222 ... line as before, confirming a clean re-seed with default scores/uncertainties and no leftover test answers.


- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - Please read the Terms of Service at: https://letsencrypt.org/documents/LE-SA-v1.7-June-04-2026.pdf You must agree in order to register with the ACME server. Do you agree? - - - - - - - - - - - - - - - - - - - - - - - - -

pasted

1:33 AM
Everything looks correct:

HTTPS is now live via certbot — https://cs-futurecities.media.mit.edu works with a valid cert, auto-renewing.
image_state and user_answers collections were dropped successfully.
The app restarted cleanly and reached Application startup complete — though the [server] scenario=... log line is missing from this tail, which is just because journalctl -n 20 only shows the last 20 lines and it may have scrolled past. Let's confirm the reseed happened correctly:
bash
mongosh survey_app --eval "db.image_state.countDocuments()"
mongosh survey_app --eval "db.user_answers.countDocuments()"
You should get 222 and 0 respectively — confirming a clean reseed with no leftover test answers.

Also, two leftover files to clean up from your earlier exports (otherwise they'll sit in user_data/ indefinitely):

bash
rm -f /home/miguelup/survey_app/user_data/user_answers.csv /home/miguelup/survey_app/user_data/image_state.json /home/miguelup/survey_app/user_data/image_state_base.csv
(only delete these if you've already downloaded copies you want, or don't need them)

If both counts check out, the app is in a clean state and ready for real use at https://cs-futurecities.media.mit.edu.


eparing to unpack .../08-python3-configargparse_1.7-2_all.deb ... Unpacking python3-configargparse (1.7-2) ... Selecting previously unselected package python3-configobj. Preparing to unpack .../09-python3-configobj_5.0.9-1_all.deb ... Unpacking python3-configobj (5.0.9-1) ... Selecting previously un

pasted

1:33 AM
Confirmed clean: image_state has 222 fresh documents (reseeded from images.csv with default TrueSkill values), user_answers is empty.

Your deployment is complete and in a clean state:

HTTPS: https://cs-futurecities.media.mit.edu — valid Let's Encrypt cert, auto-renewing
Backend: systemd service survey.service, auto-restarts, starts on boot
MongoDB: running, data bind-mounted to survey_app/user_data/mongodb, persists across reboots
Data: clean — no test answers, all 222 images at default TrueSkill scores
Optional cleanup — remove the leftover export files from the test data:

bash
rm -f /home/miguelup/survey_app/user_data/user_answers.csv /home/miguelup/survey_app/user_data/image_state.json /home/miguelup/survey_app/user_data/image_state_base.csv
You're ready to share https://cs-futurecities.media.mit.edu with real participants.

