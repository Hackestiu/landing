# Cultura Viva — landing

Astro site for [Cultura Viva](https://github.com/Hackestiu/cultura-viva-uno-q), the
offline Gaudí audio guide built on an Arduino UNO Q.

```bash
npm install
npm run dev      # http://localhost:4321
npm run build
```

## `space/`

A Hugging Face Space that runs the device's own AI pipeline in a browser, so the
"Prova-ho" section of the page can show the guide working rather than only
describing it. `space/core/` and `space/hw/` are vendored from the device repo
byte-for-byte — `space/scripts/sync_from_board.sh --check` fails if they drift.

See [`space/README.md`](space/README.md) for what is identical to the board and
what the browser stands in for, and [`space/DEPLOY.md`](space/DEPLOY.md) for
running it on a Hetzner CX22 behind Caddy. The page embeds it from `DEMO_URL`
in `src/pages/index.astro`, which is already set to `demo.culturaviva.tech`.
