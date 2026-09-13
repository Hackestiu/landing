# Deploying the demo

Two documented paths. **Fly.io** is the fast one and what the landing page
currently points at; **Hetzner** is cheaper per month and worth moving to later.

| | Fly.io | Hetzner CX22 |
|---|---|---|
| Time to a live URL | minutes | hours to days (account verification) |
| Cost | ~$12/mo at 2 GB | ~$4.59/mo, 2 vCPU / 4 GB |
| HTTPS + hostname | `*.fly.dev`, immediate | your DNS + Caddy |
| You manage | nothing | a server |

---

# Path A — Fly.io (immediate)

No DNS and no certificate work: Fly serves the app on `<app>.fly.dev` with a
valid certificate from the first deploy, so the demo has a working URL before
you own a domain record. `fly.toml` in this directory is the whole config;
`compose.yaml` and the `Caddyfile` are not used on this path.

## 1. Install and sign in

```bash
brew install flyctl
fly auth signup      # or: fly auth login
```

Signup asks for a card and then returns you to the shell ready to deploy. That
is the whole gate — there is no approval queue to sit in.

## 2. Create the app and its storage

From this directory (`space/`):

```bash
fly launch --no-deploy --copy-config --name cultura-viva-demo --region mad
```

`--no-deploy` matters: the volume and the token have to exist *before* the first
boot, or the app starts, finds no weights and no credentials, and downloads
~700 MB into a filesystem it will throw away.

If the name is taken, pick another and update `app` in `fly.toml` — the
hostname becomes `<app>.fly.dev`, which the landing page has to match.

```bash
# 5 GB holds the weights (~700 MB) and the Hub cache with room to spare.
# Fly includes the first 10 GB of volume storage.
fly volume create cv_models --size 5 --region mad

# Read token for culturaviva/park_guell-vit and culturaviva/sagrada_familia-vit.
# Stored encrypted; never goes in fly.toml or git.
fly secrets set HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxx
```

## 3. Deploy

```bash
fly deploy
```

The first deploy compiles llama.cpp on Fly's builder (~10–15 minutes; there is
no usable prebuilt wheel — the project's CPU wheel index is musl-linked and its
`libllama.so` cannot load on a Debian image). Later deploys reuse the cached
layer unless `requirements.txt` changes.

Then the first boot downloads the weights onto the volume and preloads every
stage, which is why the health check has a 20-minute grace period. Watch it:

```bash
fly logs
```

You want:

```
Stage readiness after preload: {'slm': True, 'stt': True, 'tts': True, 'vision': True}
```

`vision: True` is the one that proves `HF_TOKEN` reached the two private
classifier repos. If it says `False`, the lines above it name the reason.

Then open `https://cultura-viva-demo.fly.dev`.

## 4. If it gets OOM-killed

Measured footprint is ~1.06 GB with the SLM, Whisper and Piper warm, plus
~350 MB once a classifier loads — so 2 GB fits with roughly 600 MB to spare. If
`fly logs` shows the machine being killed during preload:

```bash
fly scale memory 4096
```

No rebuild, no redeploy — it restarts on a 4 GB machine. That raises the bill to
roughly $25/month, at which point Hetzner at $4.59 is worth the verification
wait.

## 5. Custom domain (optional, later)

`demo.culturaviva.tech` is nicer than `cultura-viva-demo.fly.dev`, but nothing
depends on it:

```bash
fly certs add demo.culturaviva.tech
```

Fly prints the DNS record to create. Add it in Netlify (**Domains →
culturaviva.tech → DNS records**), wait for `fly certs show
demo.culturaviva.tech` to report the certificate as issued, then update
`DEMO_URL` in `src/pages/index.astro`.

## Operating it

```bash
fly logs                      # follow
fly status                    # machine state, region, memory
fly deploy                    # ship a new version
fly ssh console               # shell inside the running machine
fly scale memory 4096         # more RAM, no rebuild
fly apps destroy cultura-viva-demo   # tear it all down
```

Worth knowing:

- **It is effectively single-user.** A full answer is ViT plus Whisper plus a
  ~600-token prefill and 60 tokens of decode plus Piper. `fly.toml` sets a soft
  concurrency limit of 1 so Fly queues at the edge rather than piling requests
  onto a saturated machine.
- **Do not destroy the `cv_models` volume.** It is the weights cache; without it
  the next boot re-downloads ~700 MB.
- **`auto_stop_machines` is off on purpose.** Letting the machine sleep would
  make the next visitor wait through a full model reload. Turning it on is the
  main lever if you want the bill lower than the wall-clock.

---

# Path B — Hetzner CX22 (cheaper, once verified)

Uses `compose.yaml` and the `Caddyfile` instead of `fly.toml`. About $4.59/month
for 2 vCPU and 4 GB — cheaper and faster than Fly's 2 GB machine, at the cost of
running a server yourself. Hetzner verifies new accounts before you can create
one, which can take hours or a day.

The demo is a normal Docker service: the Gradio app plus Caddy, which terminates
HTTPS. HTTPS is not optional — browsers only grant microphone access on a secure
origin, and the landing page is HTTPS, so an `http://` iframe would be blocked as
mixed content regardless.

Cost: a **CX22** (2 vCPU, 4 GB, 40 GB NVMe, 20 TB traffic) is about $4.59/month.
Hetzner bills a primary IPv4 address separately, a fraction of a euro per month.

Why this size: the pipeline is CPU-bound and its weakest point is latency, so the
second core matters more than anything else you could spend on. See the memory
figures below for why 4 GB rather than 2.

### Server

Hetzner Cloud Console → **Add Server**:

| | |
|---|---|
| Location | Falkenstein, Nuremberg or Helsinki — all EU, closest to Barcelona |
| Image | Ubuntu 24.04 |
| Type | Shared vCPU, **CX22** (2 vCPU / 4 GB) |
| SSH key | add yours at creation — do not fall back to a root password |

Measured footprint, reading the process's own RSS after each preload step
(x86 container, no classifier available at the time):

| After loading | RSS | peak |
|---|---|---|
| baseline | 17 MB | 17 MB |
| + gradio | 118 MB | 118 MB |
| + SLM (Qwen2.5-0.5B q4, `use_mlock=True`) | 677 MB | 677 MB |
| + faster-whisper base.en int8 | 904 MB | 982 MB |
| + Piper ×2 | **1058 MB** | 1058 MB |
| after answering one question | 1074 MB | 1074 MB |

Serving a question costs 16 MB on top of the preloaded total, so the footprint is
flat in use — what you provision for is startup, not load. Add roughly 350 MB for
a site's ViT classifier once `HF_TOKEN` is set, so budget about **1.4 GB
resident** with everything warm. `app.py` preloads every stage at boot by design,
so that is the steady state from startup.

2 GB would fit on paper; 4 GB is the recommendation because ~600 MB of headroom
leaves nothing for a spike, and because the SLM is loaded with `use_mlock=True`,
which pins its weights in RAM and deliberately defeats swap — so swap will not
save a box that runs short.

**Confirm this on the server rather than trusting the table.** The figures above
come from an emulated x86 container on a contended laptop; treat them as the
right order of magnitude, not as your machine's numbers. `docker stats` after the
first successful boot is the measurement that counts.

### DNS

`culturaviva.tech` is on Netlify DNS (NS1 nameservers), so the record goes in
the Netlify dashboard under **Domains → culturaviva.tech → DNS records**:

```
demo    A    <server-ip>
```

Add it **before** the first `docker compose up`. Caddy requests the certificate
on boot, and issuance fails if the name does not yet resolve to the server.

### Prepare the server

```bash
ssh root@<server-ip>

# Docker
curl -fsSL https://get.docker.com | sh

# 2 GB of swap. Not a substitute for RAM here (the SLM is mlock'd and cannot be
# swapped), but it absorbs transient spikes during the llama.cpp compile.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
```

Hetzner also offers a Cloud Firewall in the console, applied before traffic
reaches the machine. Using both is reasonable; using only the console one is
fine too, as long as 22, 80 and 443 are open.

### Configure and start

```bash
git clone https://github.com/Hackestiu/landing.git
cd landing/space

cp .env.example .env
nano .env          # set DEMO_DOMAIN and HF_TOKEN

docker compose up -d --build
```

`HF_TOKEN` must be a read token with access to `culturaviva/park_guell-vit` and
`culturaviva/sagrada_familia-vit`. Both are private; without it the app still
boots but every photo comes back unclassified.

Two things take time on the first run, and neither is a hang:

- **The build compiles llama.cpp** (roughly 10–15 minutes on 2 vCPU). There is
  no usable prebuilt wheel — the project's CPU wheel index is musl-linked and
  its `libllama.so` will not load on a Debian image.
- **The first boot downloads ~700 MB** of weights into the `models` volume, then
  preloads every stage. Subsequent restarts reuse the volume and start in
  seconds.

Watch it come up:

```bash
docker compose logs -f app
```

You want to reach `Stage readiness after preload: {'slm': True, 'stt': True,
'tts': True, 'vision': True}`. Any `False` there names a stage that failed —
the lines above it say why.

### Point the landing page at it

`src/pages/index.astro` already points at `https://demo.culturaviva.tech`, so
there is nothing to change — but note that the landing page is on Netlify and
builds automatically from a push to the deployed branch. Merging the demo
section before the server answers means publishing a section whose iframe does
not load.

The embed is click-to-load, so a visitor who never presses the button sees no
error either way. Still, merge after the server is serving, not before.

### Operating it

```bash
docker compose logs -f app          # follow
docker compose restart app          # restart, keeps the weights
git pull && docker compose up -d --build   # deploy a new version
docker system prune -af             # reclaim disk after several rebuilds
docker stats --no-stream            # real memory use, once warm
```

Things worth knowing before they bite:

- **It is effectively single-user.** A full answer is ViT plus Whisper plus a
  ~600-token prefill and 60 tokens of decode plus Piper. Gradio queues
  concurrent visitors; the second one waits for the first.
- **Never delete the `caddy_data` volume.** It holds the issued certificate, and
  Let's Encrypt rate-limits repeat issuance for the same name.
- **The `models` volume is the cache.** Removing it means re-downloading 700 MB
  on the next start.
- **Unattended upgrades.** `apt install unattended-upgrades` is worth the two
  minutes on a box that will sit exposed for months.

## Running it locally first

Worth doing before touching a server:

```bash
docker build -t cultura-viva-demo .
docker run --rm -p 7860:7860 -e HF_TOKEN=hf_... -v cv_models:/app/models cultura-viva-demo
# http://localhost:7860
```

No Caddy, no domain, no certificate — localhost counts as a secure origin, so
the microphone works there too.
