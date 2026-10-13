#!/usr/bin/env python3
"""Builds jvr0x.com in place: shared head tags, header/footer chrome, data-driven cards, sitemap.

The committed HTML is the served output (GitHub Pages serves the repo root as-is, no build on
GitHub), so run this after editing partials/, data/ or a page, and commit the result:

    python3 tools/build.py            # offline, idempotent
    python3 tools/build.py --recipes  # first refreshes data/recipes.json and
                                      # data/bench-recipes.json from GitHub via `gh api`
    python3 tools/build.py --models   # first refreshes data/models.json from the
                                      # Hugging Face API

Marker blocks rewritten in each page listed in data/pages.json (with a "file"):
    <!-- jx:head -->        ... <!-- /jx:head -->        title, description, canonical, OG/Twitter
    <!-- jx:header -->      ... <!-- /jx:header -->      partials/header.html
    <!-- jx:footer -->      ... <!-- /jx:footer -->      partials/footer.html
    <!-- jx:cards NAME [list] --> ... <!-- /jx:cards --> data/NAME.json rendered as cards or rows
Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://jvr0x.com"
DEFAULT_OG_IMAGE = f"{SITE}/assets/og-default.png"

AI_MODELS_REPO = "jvr0x/ai-models"
BENCH_REPO = "jvr0x/dgx-spark-bench"

HF_AUTHOR = "jvr0x"
HF_API = "https://huggingface.co/api/models"
HF_FIELDS = ("cardData", "downloads", "lastModified", "likes", "library_name", "pipeline_tag", "siblings", "tags")
HF_MAX_TAGS = 4
# Reason: Hugging Face tags are machine names; these are the ones worth a card, in card order.
HF_TAG_LABELS = (
    ("gguf", "GGUF"),
    ("exllamav3", "EXL3"),
    ("safetensors", "Safetensors"),
    ("lora", "LoRA"),
    ("unsloth", "Unsloth"),
    ("agent", "Agent"),
    ("tool-use", "Tool use"),
    ("distillation", "Distilled"),
    ("dgx-spark", "DGX Spark"),
)


class BuildError(Exception):
    """Raised when a page or data file can't be built (bad markers or bad data)."""


# ── Files ────────────────────────────────────────────────────────────────────


def read_text(path: Path) -> str:
    """Reads a UTF-8 file without newline translation."""
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def write_if_changed(path: Path, text: str) -> bool:
    """Writes text to path only when it differs; returns True if the file changed."""
    if path.exists() and read_text(path) == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
    return True


def load_json_list(path: Path) -> list:
    """Loads a JSON file that must contain a list."""
    try:
        data = json.loads(read_text(path))
    except FileNotFoundError as e:
        raise BuildError(f"{path.name}: data file not found") from e
    except json.JSONDecodeError as e:
        raise BuildError(f"{path.name}: invalid JSON ({e})") from e
    if not isinstance(data, list):
        raise BuildError(f"{path.name}: expected a JSON list")
    return data


# ── Marker blocks ────────────────────────────────────────────────────────────


def replace_block(text: str, name: str, body: str, where: str) -> str:
    """Replaces everything between <!-- jx:NAME --> and <!-- /jx:NAME --> with body.

    The markers themselves are kept. Raises BuildError if the block is missing,
    unclosed, or appears more than once.
    """
    start, end = f"<!-- jx:{name} -->", f"<!-- /jx:{name} -->"
    n_start, n_end = text.count(start), text.count(end)
    if n_start == 0 and n_end == 0:
        raise BuildError(f"{where}: missing {start} ... {end} block")
    if n_start != 1 or n_end != 1:
        raise BuildError(f"{where}: expected exactly one {start} and one {end}, found {n_start}/{n_end}")
    i, j = text.index(start), text.index(end)
    if j < i:
        raise BuildError(f"{where}: {end} appears before {start}")
    return text[: i + len(start)] + body + text[j:]


def partial_body(path: Path, name: str) -> str:
    """Returns the content of a partial between its own jx:NAME markers.

    Reason: the partials carry the markers too (so CHROME.md can be pasted verbatim);
    only the inner part is spliced into pages.
    """
    text = read_text(path)
    start, end = f"<!-- jx:{name} -->", f"<!-- /jx:{name} -->"
    if start not in text or end not in text:
        raise BuildError(f"{path.name}: partial must contain {start} and {end}")
    return text[text.index(start) + len(start) : text.index(end)]


CARDS_RE = re.compile(r"<!-- jx:cards ([a-z0-9-]+)(?: (grid|list))? -->(.*?)<!-- /jx:cards -->", re.DOTALL)


def fill_cards(text: str, data_dir: Path, where: str) -> str:
    """Renders every <!-- jx:cards NAME [grid|list] --> block in text from data/NAME.json."""
    if text.count("<!-- jx:cards ") != text.count("<!-- /jx:cards -->"):
        raise BuildError(f"{where}: unbalanced jx:cards markers")

    def render(m: re.Match) -> str:
        name, layout = m.group(1), m.group(2) or "grid"
        items = validate_items(load_json_list(data_dir / f"{name}.json"), f"{name}.json")
        renderer = render_row if layout == "list" else render_card
        inner = "\n".join(renderer(item) for item in items)
        opener = f"<!-- jx:cards {name}{' list' if layout == 'list' else ''} -->"
        return f"{opener}\n{inner}\n<!-- /jx:cards -->"

    return CARDS_RE.sub(render, text)


# ── Card data ────────────────────────────────────────────────────────────────


def check_url(url: object, where: str) -> str:
    """Returns url if it is an absolute http(s) URL or a root-relative path, else raises."""
    if not isinstance(url, str) or not re.match(r"^(https?://[^\s\"<>]+|/[^\s\"<>]*)$", url):
        raise BuildError(f"{where}: invalid url {url!r}")
    return url


def validate_items(items: list, where: str) -> list[dict]:
    """Validates card entries: title/url required; description/meta str; tags list[str]; links list."""
    for i, item in enumerate(items):
        at = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise BuildError(f"{at}: entry must be an object")
        for key in ("title", "url"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise BuildError(f"{at}: missing required '{key}'")
        check_url(item["url"], at)
        for key in ("description", "meta"):
            if key in item and not isinstance(item[key], str):
                raise BuildError(f"{at}: '{key}' must be a string")
        if "hf" in item and (not isinstance(item["hf"], str) or not item["hf"].strip()):
            raise BuildError(f"{at}: 'hf' must be a non-empty Hugging Face repo name")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise BuildError(f"{at}: 'tags' must be a list of strings")
        links = item.get("links", [])
        if not isinstance(links, list):
            raise BuildError(f"{at}: 'links' must be a list")
        for link in links:
            if not isinstance(link, dict) or not isinstance(link.get("label"), str):
                raise BuildError(f"{at}: each link needs a 'label' and 'url'")
            check_url(link.get("url"), at)
    return items


def esc(value: str) -> str:
    """HTML-escapes text and attribute values."""
    return html.escape(value, quote=True)


def anchor(url: str, label: str, cls: str = "") -> str:
    """Renders a link; external links get rel=noopener."""
    rel = ' rel="noopener"' if url.startswith("http") else ""
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<a{cls_attr} href="{esc(url)}"{rel}>{esc(label)}</a>'


def render_extras(item: dict, indent: str) -> list[str]:
    """Renders the optional meta line, tags and extra links of an entry."""
    out: list[str] = []
    if item.get("meta"):
        out.append(f'{indent}<p class="card-meta">{esc(item["meta"])}</p>')
    if item.get("tags"):
        tags = "".join(f'<span class="tag sm">{esc(t)}</span>' for t in item["tags"])
        out.append(f'{indent}<div class="tags">{tags}</div>')
    if item.get("links"):
        links = "".join(anchor(link["url"], link["label"]) for link in item["links"])
        out.append(f'{indent}<div class="card-links">{links}</div>')
    return out


def render_card(item: dict) -> str:
    """Renders one entry as a card.

    Reason: a card is a <div> with a title link, not a wrapping <a>, because entries can
    carry their own links and nested anchors are invalid HTML.
    """
    opening = f'<div class="card fade-in" data-hf="{esc(item["hf"])}">' if item.get("hf") else '<div class="card fade-in">'
    lines = [
        opening,
        f'  <h3 class="card-title">{anchor(item["url"], item["title"])}</h3>',
    ]
    if item.get("description"):
        lines.append(f'  <p class="card-desc">{esc(item["description"])}</p>')
    lines += render_extras(item, "  ")
    lines.append("</div>")
    return "\n".join(lines)


def render_row(item: dict) -> str:
    """Renders one entry as a compact row (for long lists such as recipes)."""
    lines = [
        '<div class="row">',
        f'  <h3 class="row-title">{anchor(item["url"], item["title"])}</h3>',
    ]
    if item.get("description"):
        lines.append(f'  <p class="row-desc">{esc(item["description"])}</p>')
    lines += render_extras(item, "  ")
    lines.append("</div>")
    return "\n".join(lines)


# ── Pages: head tags + sitemap ───────────────────────────────────────────────


def load_pages(data_dir: Path) -> list[dict]:
    """Loads data/pages.json: [{path, file?, title?, description?, image?}], path like '/about/'."""
    pages = load_json_list(data_dir / "pages.json")
    for i, page in enumerate(pages):
        at = f"pages.json[{i}]"
        path = page.get("path") if isinstance(page, dict) else None
        if not isinstance(path, str) or not path.startswith("/") or not path.endswith("/"):
            raise BuildError(f"{at}: 'path' must start and end with '/'")
        if "file" in page:
            for key in ("title", "description"):
                if not isinstance(page.get(key), str) or not page[key].strip():
                    raise BuildError(f"{at}: pages with a file need '{key}'")
    return pages


def render_head(page: dict) -> str:
    """Renders <title>, description, canonical and the full OG/Twitter set for a page."""
    url = SITE + page["path"]
    image = page.get("image", DEFAULT_OG_IMAGE)
    title, desc = esc(page["title"]), esc(page["description"])
    tags = [
        f"<title>{title}</title>",
        f'<meta name="description" content="{desc}">',
        f'<link rel="canonical" href="{esc(url)}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="jvr0x">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:url" content="{esc(url)}">',
        f'<meta property="og:image" content="{esc(image)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="twitter:site" content="@jvr0x">',
        '<meta name="twitter:creator" content="@jvr0x">',
        f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{desc}">',
        f'<meta name="twitter:image" content="{esc(image)}">',
    ]
    return "\n" + "\n".join(tags) + "\n"


def render_sitemap(pages: list[dict]) -> str:
    """Renders sitemap.xml for every page path (no lastmod, so output is stable)."""
    urls = "".join(f"  <url><loc>{esc(SITE + p['path'])}</loc></url>\n" for p in pages)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )


def build_page(text: str, page: dict, header: str, footer: str, data_dir: Path) -> str:
    """Returns the page text with head, header, footer and card blocks rendered."""
    where = page["file"]
    text = replace_block(text, "head", render_head(page), where)
    text = replace_block(text, "header", header, where)
    text = replace_block(text, "footer", footer, where)
    return fill_cards(text, data_dir, where)


def build(root: Path = ROOT) -> list[str]:
    """Builds every page under root in place; returns the relative paths that changed.

    Reason: all outputs are rendered before anything is written, so a bad page or data
    file aborts the build without leaving the site half-updated.
    """
    data_dir = root / "data"
    pages = load_pages(data_dir)
    header = partial_body(root / "partials" / "header.html", "header")
    footer = partial_body(root / "partials" / "footer.html", "footer")
    outputs: dict[str, str] = {}
    for page in pages:
        if "file" not in page:
            continue  # served by another repo (project page), sitemap only
        path = root / page["file"]
        if not path.exists():
            raise BuildError(f"{page['file']}: page listed in pages.json does not exist")
        outputs[page["file"]] = build_page(read_text(path), page, header, footer, data_dir)
    outputs["sitemap.xml"] = render_sitemap(pages)
    return [rel for rel, text in outputs.items() if write_if_changed(root / rel, text)]


# ── --recipes: regenerate recipe data from GitHub ────────────────────────────


def gh_api(path: str, raw: bool = False) -> str:
    """Calls `gh api` and returns stdout (raw file contents when raw=True)."""
    cmd = ["gh", "api", path]
    if raw:
        cmd[2:2] = ["-H", "Accept: application/vnd.github.raw"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(f"gh api {path} failed: {result.stderr.strip()}")
    return result.stdout


def clean_copy(text: str) -> str:
    """Normalizes upstream text for site copy (no em dashes, single spaces)."""
    return re.sub(r"\s+", " ", text.replace("\N{EM DASH}", "-")).strip()


def parse_recipe_yaml(text: str) -> dict:
    """Extracts runtime, model, display_name and the header comment from an lmswitch recipe.

    Minimal parser for the flat top-level keys we need (stdlib only, no PyYAML).
    Reason: some recipes repeat a key (e.g. `runtime: llama` then `runtime: "vllm"`);
    lmswitch loads them with PyYAML, which keeps the last value, so the last one wins here too.
    """
    out: dict = {}
    header: list[str] = []
    in_header = True
    for line in text.splitlines():
        if in_header and line.startswith("#"):
            header.append(line.lstrip("#").strip())
            continue
        in_header = False
        m = re.match(r"^(runtime|model|display_name):\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value[:1] in "\"'":
            q = value[0]
            value = value[1 : value.find(q, 1)] if value.find(q, 1) > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        out[m.group(1)] = value
    # First header paragraph, up to the first blank comment line.
    para: list[str] = []
    for h in header:
        if not h:
            break
        para.append(h)
    # Reason: header comments often continue with changelogs; the first sentence says what it is.
    summary = clean_copy(" ".join(para))
    out["summary"] = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", summary, maxsplit=1)[0] if summary else ""
    return out


def fetch_ai_models() -> list[dict]:
    """Builds recipe card entries for every *.yaml at the root of jvr0x/ai-models main."""
    listing = json.loads(gh_api(f"repos/{AI_MODELS_REPO}/contents?ref=main"))
    entries = []
    for f in sorted(listing, key=lambda f: f["name"]):
        if f["type"] != "file" or not f["name"].endswith(".yaml"):
            continue
        info = parse_recipe_yaml(gh_api(f"repos/{AI_MODELS_REPO}/contents/{f['name']}?ref=main", raw=True))
        stem = f["name"][: -len(".yaml")]
        # Reason: a few bench profiles point at a local ~/models/ path; show the public id.
        model = re.sub(r"^/home/[^/]+/models/", "", info.get("model", "?"))
        entry = {
            "id": stem,
            "title": info.get("display_name") or stem,
            "url": f"https://github.com/{AI_MODELS_REPO}/blob/main/{f['name']}",
            "meta": f"{f['name']} · {info.get('runtime', '?')} · {model}",
        }
        if info["summary"]:
            entry["description"] = info["summary"]
        entries.append(entry)
    return entries


def fetch_bench(ai_model_stems: set[str]) -> list[dict]:
    """Builds bench-config entries from jvr0x/dgx-spark-bench results/*.json.

    Only measured series with a recipe_url are listed, one entry per recipe_url.
    Reason: a bench config is cross-linked to an ai-models recipe only on an exact name
    match (a yaml filename inside the bench recipe dir, else the dir name, equals an
    ai-models stem);
    near-misses are left unlinked rather than guessed.
    """
    tree = json.loads(gh_api(f"repos/{BENCH_REPO}/git/trees/main?recursive=1"))["tree"]
    dir_yamls: dict[str, set[str]] = {}
    result_files = []
    for node in tree:
        parts = node["path"].split("/")
        if len(parts) == 3 and parts[0] == "recipes" and parts[2].endswith(".yaml") and parts[2] != "harness.yaml":
            dir_yamls.setdefault(parts[1], set()).add(parts[2][: -len(".yaml")])
        if len(parts) == 2 and parts[0] == "results" and parts[1].endswith(".json"):
            result_files.append(node["path"])

    entries: list[dict] = []
    seen: set[str] = set()
    # Reason: "-nothinking" runs share a recipe with the main run; let the main run name the entry.
    for path in sorted(result_files, key=lambda p: ("nothinking" in p, p)):
        if path == "results/sample-spark.json":
            continue
        data = json.loads(gh_api(f"repos/{BENCH_REPO}/contents/{path}?ref=main", raw=True))
        for s in data.get("series", []):
            url = s.get("recipe_url")
            if s.get("status") != "measured" or not url or url in seen:
                continue
            seen.add(url)
            local = re.match(rf"^https://github\.com/{BENCH_REPO}/tree/main/recipes/([^/]+)/?$", url)
            entry = {
                "id": s["id"],
                "title": clean_copy(s.get("model") or s["id"]),
                "url": url,
                "meta": " · ".join(clean_copy(str(x)) for x in (s.get("quant"), s.get("backend")) if x),
            }
            if local:
                # Prefer the yaml actually inside the bench dir; fall back to the dir name.
                matches = sorted(dir_yamls.get(local.group(1), set()) & ai_model_stems)
                if not matches and local.group(1) in ai_model_stems:
                    matches = [local.group(1)]
                links = [
                    {"label": f"lmswitch: {m}.yaml", "url": f"https://github.com/{AI_MODELS_REPO}/blob/main/{m}.yaml"}
                    for m in matches
                ]
                if links:
                    entry["links"] = links
            else:
                owner = url.split("/")[3]
                entry["description"] = f"Upstream recipe by {owner}, measured on the bench."
            entries.append(entry)
    return entries


def refresh_recipes(data_dir: Path) -> None:
    """Regenerates data/recipes.json and data/bench-recipes.json from GitHub."""
    recipes = fetch_ai_models()
    bench = fetch_bench({r["id"] for r in recipes})
    for name, items in (("recipes", recipes), ("bench-recipes", bench)):
        validate_items(items, f"{name}.json")
        write_if_changed(data_dir / f"{name}.json", json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    print(f"recipes: {len(recipes)} lmswitch recipes, {len(bench)} bench configs")


# ── --models: regenerate model data from Hugging Face ────────────────────────


def hf_get(url: str) -> object:
    """GETs a Hugging Face API URL and returns the parsed JSON."""
    request = urllib.request.Request(url, headers={"User-Agent": "jvr0x.com site build (tools/build.py)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (OSError, ValueError) as e:
        raise BuildError(f"{url} failed: {e}") from e


def fetch_hf_models(author: str = HF_AUTHOR) -> list[dict]:
    """Public models of an author, newest first, with the fields a card needs."""
    query = "".join(f"&expand[]={field}" for field in HF_FIELDS)
    models = hf_get(f"{HF_API}?author={quote_plus(author)}{query}&sort=lastModified&direction=-1")
    if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
        raise BuildError(f"{HF_API}: expected a list of models")
    return [
        m
        for m in models
        if isinstance(m.get("id"), str) and m["id"].startswith(f"{author}/") and not m.get("private") and not m.get("gated")
    ]


def _hf_tags(model: dict) -> list[str]:
    """The string tags of a model entry."""
    return [t for t in (model.get("tags") or []) if isinstance(t, str)]


def _hf_base(model: dict, author: str = HF_AUTHOR) -> str:
    """Base model id from the base_model tags; the finetune/quantized forms name it best.

    A base under our own author is named without the owner: the card already sits on that profile.
    """
    tags = _hf_tags(model)
    for prefix in ("base_model:finetune:", "base_model:quantized:", "base_model:"):
        for tag in tags:
            if tag.startswith(prefix):
                base = tag[len(prefix) :]
                return base[len(author) + 1 :] if author and base.startswith(f"{author}/") else base
    return ""


def _hf_quantisations(model: dict) -> list[str]:
    """Quants the repo ships: GGUF file suffixes, else the bit-depth tag."""
    quants: list[str] = []
    for sibling in model.get("siblings") or []:
        name = sibling.get("rfilename", "") if isinstance(sibling, dict) else ""
        m = re.search(r"\.([A-Za-z0-9_]+)\.gguf$", name)
        if m and m.group(1) not in quants:
            quants.append(m.group(1))
    return quants or [t for t in _hf_tags(model) if re.fullmatch(r"\d+-bit", t)]


def _hf_format(model: dict) -> str:
    """The serving format shown on the card."""
    tags = set(_hf_tags(model))
    library = model.get("library_name") or ""
    if "gguf" in tags or library == "gguf":
        return "GGUF"
    if "exllamav3" in tags or "exl3" in tags or library == "exllamav3":
        return "EXL3"
    if library == "transformers" or "safetensors" in tags:
        return "Safetensors"
    return library or "Model"


def _hf_license(model: dict) -> str:
    """License id from the license: tag."""
    for tag in _hf_tags(model):
        if tag.startswith("license:"):
            return tag[len("license:") :]
    return ""


def describe_hf_model(model: dict) -> str:
    """Card copy for a model with no hand-written description, from its tags alone."""
    tags = set(_hf_tags(model))
    base = _hf_base(model)
    quantised = any(t.startswith("base_model:quantized:") for t in _hf_tags(model))
    fmt = _hf_format(model)
    if base and quantised:
        lead = f"{fmt} build of {base}"
    elif base:
        lead = f"{fmt} fine-tune of {base}"
    else:
        lead = f"{fmt} model"
    if "lora" in tags and "unsloth" in tags:
        tuning = "LoRA-tuned with Unsloth"
    elif "unsloth" in tags:
        tuning = "Tuned with Unsloth"
    elif "lora" in tags:
        tuning = "LoRA-tuned"
    else:
        tuning = ""
    if tuning and tags & {"agent", "tool-use"}:
        tuning += " for agent tool use"
    sentences = [lead + "."]
    if tuning:
        sentences.append(tuning + ".")
    if model.get("pipeline_tag") == "image-text-to-text":
        sentences.append("Takes text and image inputs.")
    if "dgx-spark" in tags:
        sentences.append("Quantised on one DGX Spark." if quantised else "Trained on one DGX Spark.")
    return " ".join(sentences)


def hf_meta(model: dict) -> str:
    """The format / quants / base / license line under a card title."""
    parts = [_hf_format(model)]
    quants = _hf_quantisations(model)
    if quants:
        parts.append(" / ".join(quants))
    base = _hf_base(model)
    if base:
        parts.append(f"base {base}")
    if _hf_license(model):
        parts.append(_hf_license(model))
    return " · ".join(parts)


def hf_tags(model: dict) -> list[str]:
    """Up to HF_MAX_TAGS card tags, in HF_TAG_LABELS order.

    Reason: an EXL3 repo also carries the safetensors tag, so the Safetensors label only
    counts when the format really is Safetensors.
    """
    tags = set(_hf_tags(model))
    fmt = _hf_format(model)
    out = [label for tag, label in HF_TAG_LABELS if tag in tags]
    out = [label for label in out if label != "Safetensors" or fmt == "Safetensors"]
    if fmt not in out:
        out.insert(0, fmt)
    return out[:HF_MAX_TAGS]


def hf_entry(model: dict, curated: dict | None = None) -> dict:
    """Card entry for one Hugging Face repo.

    Reason: curated copy (hand-written title/description/meta/tags/links, looked up by repo name)
    wins where present, because what the site says about its own releases beats what the tags say.
    The url always comes from Hugging Face, so the card cannot point at a renamed repo.
    """
    name = model["id"].split("/")[-1]
    entry = {
        "hf": name,
        "title": name,
        "url": f"https://huggingface.co/{model['id']}",
        "description": describe_hf_model(model),
        "meta": hf_meta(model),
        "tags": hf_tags(model),
        "links": [{"label": "Hugging Face", "url": f"https://huggingface.co/{model['id']}"}],
    }
    for key in ("title", "description", "meta", "tags", "links"):
        if curated and curated.get(key):
            entry[key] = curated[key]
    return entry


def refresh_models(data_dir: Path, author: str = HF_AUTHOR) -> None:
    """Regenerates data/models.json from the Hugging Face API, newest first.

    Reason: publishing on Hugging Face has to be enough for a model to show up on the site.
    Committed entries are matched to repos by their `hf` name: a hand-written entry keeps its
    copy instead of the tag-derived one, and entries without `hf` (not from Hugging Face) are
    kept as they are.
    """
    committed = load_json_list(data_dir / "models.json")
    curated = {
        e["hf"]: e for e in committed if isinstance(e, dict) and isinstance(e.get("hf"), str) and e["hf"]
    }
    hand_made = [e for e in committed if isinstance(e, dict) and not e.get("hf")]
    entries: list[dict] = []
    hand = 0
    for model in fetch_hf_models(author):
        entry = curated.pop(model["id"].split("/")[-1], None)
        if entry:
            hand += 1
        entries.append(hf_entry(model, entry))
    entries.extend(hand_made)
    validate_items(entries, "models.json")
    write_if_changed(data_dir / "models.json", json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    print(f"models: {len(entries)} cards from Hugging Face, {hand} with hand-written copy")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recipes", action="store_true", help="refresh recipe data from GitHub first (needs gh)")
    parser.add_argument("--models", action="store_true", help="refresh data/models.json from the Hugging Face API first")
    args = parser.parse_args(argv)
    try:
        if args.recipes:
            refresh_recipes(ROOT / "data")
        if args.models:
            refresh_models(ROOT / "data")
        changed = build(ROOT)
    except BuildError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    print("changed: " + (", ".join(changed) if changed else "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
