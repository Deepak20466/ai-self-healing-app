# Exposing a local webhook to GitHub, without Docker or a cloud VM

`ci-failure.yml` and `deploy.yml` POST to `HEALER_WEBHOOK_URL` (sentinel-pod's
`POST /webhooks/ci`). If you're demoing the full loop from a laptop instead
of a provisioned cloud VM, GitHub's servers can't reach `localhost:8002`
directly — you need a public URL that forwards to it.

## Lightest option: an SSH reverse tunnel to any box you can SSH into

If you have *any* small VM/host with a public IP and SSH access (a free-tier
box is enough — it only relays traffic, no app code runs there), forward its
port 8002 back to your laptop's sentinel-pod:

```bash
ssh -R 8002:localhost:8002 -N you@your-public-host
```

Then set `HEALER_WEBHOOK_URL=http://your-public-host:8002/webhooks/ci` in
`.env` (and configure the corresponding GitHub Actions secret,
`HEALER_WEBHOOK_URL`, to point at the same address). Add `-o
ServerAliveInterval=30` to keep the tunnel from being dropped by an idle
timeout during a demo.

## No spare box at all: a temporary public tunnel

Tools like `ssh -R` against a public relay service, or a similar reverse-proxy
tunnel utility, work the same way without needing your own second server —
pick whichever one you're comfortable trusting with traffic, since it's
inline between GitHub and your machine. None of this requires Docker or
changes anything about the app itself: the webhook endpoint doesn't know or
care whether it was reached directly or through a tunnel.

## Recommended for anything beyond a quick demo

Just run `scripts/provision_vm.sh` against a real (even free-tier) Ubuntu VM
and set `PUBLIC_URL`/`HEALER_WEBHOOK_URL` to that VM's own address — all 4
pods run there per SPEC.md's "CLOUD DEPLOYMENT" section, so GitHub reaches
the webhook directly with no tunnel at all, and the deploy stops depending on
your laptop staying online.
