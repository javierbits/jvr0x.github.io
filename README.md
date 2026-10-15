# jvr0x.github.io
![RepoBeagle](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/jvr0x/jvr0x.github.io/repobeagle/badge.json)

Source of [jvr0x.com](https://jvr0x.com): Javier's site about local AI on DGX Spark (models,
serving recipes, benchmarks, tools, the AGI Checkpoint newsletter) plus the CV at
[/about/](https://jvr0x.com/about/).

Plain HTML, CSS and a little JS in a terminal style, served by GitHub Pages from the root of
`main`. A small stdlib Python script stamps the shared header/footer, head tags and data-driven
cards into the pages; the committed HTML is what gets served.

## Build

```bash
python3 tools/build.py              # re-render marker blocks + sitemap.xml (offline, idempotent)
python3 tools/build.py --recipes    # also refresh recipe data from GitHub (needs gh)
```

Adding a model, bot or tool is one entry in `data/*.json` plus a build. Details for humans and
agents: [AGENTS.md](AGENTS.md).

## Preview

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

Serve from the repo root: pages use root-relative URLs (`/style.css`, `/assets/chrome.css`).
Project pages such as `/dgx-spark-bench/` come from their own repos and 404 locally.

## Tests and checks

```bash
python -m pytest tests/             # build script tests (pytest)
python3 tools/check_site.py         # meta tags, internal links, OG image size
python tools/shots.py http://localhost:8000 /tmp/shots   # overflow + screenshots (Playwright)
```

## License

MIT © 2026 jvr0x.
