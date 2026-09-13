# Deploying the demo on a Hetzner CX22

The demo is a normal Docker service: the Gradio app plus Caddy, which terminates
HTTPS. HTTPS is not optional — browsers only grant microphone access on a secure
origin, and the landing page is HTTPS, so an `http://` iframe would be blocked as
mixed content regardless.

Cost: a **CX22** (2 vCPU, 4 GB, 40 GB NVMe, 20 TB traffic) is about $4.59/month.
Hetzner bills a primary IPv4 address separately, a fraction of a euro per month.

Why this size: the pipeline is CPU-bound and its weakest point is latency, so the
second core matters more than anything else you could spend on. See the memory
figures below for why 4 GB rather than 2.

## 1. Server

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

## 2. DNS

`culturaviva.tech` is on Netlify DNS (NS1 nameservers), so the record goes in
the Netlify dashboard under **Domains → culturaviva.tech → DNS records**:

```
demo    A    <server-ip>
```

Add it **before** the first `docker compose up`. Caddy requests the certificate
on boot, and issuance fails if the name does not yet resolve to the server.

## 3. Prepare the server

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

## 4. Configure and start

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

## 5. Point the landing page at it

`src/pages/index.astro` already points at `https://demo.culturaviva.tech`, so
there is nothing to change — but note that the landing page is on Netlify and
builds automatically from a push to the deployed branch. Merging the demo
section before the server answers means publishing a section whose iframe does
not load.

The embed is click-to-load, so a visitor who never presses the button sees no
error either way. Still, merge after the server is serving, not before.

## Operating it

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
