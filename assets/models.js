/*
 * Models page: renders the Hugging Face catalogue live, so publishing a model is enough for
 * it to appear on jvr0x.com/models/ without a site rebuild.
 *
 * The committed cards (tools/build.py --models) are the crawlable, no-JS baseline. This script
 * refines that list once the request comes back:
 * - a card whose [data-hf] repo is still on Hugging Face keeps its hand-written copy,
 * - a repo the build did not know about gets a card derived from its tags,
 * - a card whose repo is gone is dropped.
 * On any failure the committed cards are left exactly as they are.
 *
 * The card markup mirrors build.py render_card(): a div, a title link, and text-only nodes
 * (everything HF returns goes through textContent, never innerHTML).
 */
(() => {
  "use strict";

  const grid = document.querySelector("[data-hf-models]");
  if (!grid) return;

  const author = grid.getAttribute("data-hf-models") || "javierbits";
  const API = "https://huggingface.co/api/models";
  const FIELDS = ["cardData", "downloads", "lastModified", "likes", "library_name", "pipeline_tag", "siblings", "tags"];
  const TAG_LABELS = [
    ["gguf", "GGUF"],
    ["exllamav3", "EXL3"],
    ["safetensors", "Safetensors"],
    ["lora", "LoRA"],
    ["unsloth", "Unsloth"],
    ["agent", "Agent"],
    ["tool-use", "Tool use"],
    ["distillation", "Distilled"],
    ["dgx-spark", "DGX Spark"],
  ];
  const MAX_TAGS = 4;

  // Reason: script.js observes the committed cards at parse time and never sees these, so
  // generated cards need their own observer (same options) to fade in like the rest.
  const fade = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          fade.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.1, rootMargin: "0px 0px -40px 0px" }
  );

  const tagList = (model) => (model.tags || []).filter((t) => typeof t === "string");

  const baseModel = (model) => {
    const tags = tagList(model);
    for (const prefix of ["base_model:finetune:", "base_model:quantized:", "base_model:"]) {
      const hit = tags.find((t) => t.startsWith(prefix));
      if (hit) {
        const base = hit.slice(prefix.length);
        return base.startsWith(`${author}/`) ? base.slice(author.length + 1) : base;
      }
    }
    return "";
  };

  const quantisations = (model) => {
    const quants = [];
    for (const sibling of model.siblings || []) {
      const name = (sibling && sibling.rfilename) || "";
      const m = /\.([A-Za-z0-9_]+)\.gguf$/.exec(name);
      if (m && !quants.includes(m[1])) quants.push(m[1]);
    }
    return quants.length ? quants : tagList(model).filter((t) => /^\d+-bit$/.test(t));
  };

  const format = (model) => {
    const tags = new Set(tagList(model));
    const library = model.library_name || "";
    if (tags.has("gguf") || library === "gguf") return "GGUF";
    if (tags.has("exllamav3") || tags.has("exl3") || library === "exllamav3") return "EXL3";
    if (library === "transformers" || tags.has("safetensors")) return "Safetensors";
    return library || "Model";
  };

  const license = (model) => {
    const hit = tagList(model).find((t) => t.startsWith("license:"));
    return hit ? hit.slice("license:".length) : "";
  };

  const description = (model) => {
    const tags = new Set(tagList(model));
    const base = baseModel(model);
    const quantised = tagList(model).some((t) => t.startsWith("base_model:quantized:"));
    const fmt = format(model);
    const lead =
      base && quantised ? `${fmt} build of ${base}` : base ? `${fmt} fine-tune of ${base}` : `${fmt} model`;
    let tuning = "";
    if (tags.has("lora") && tags.has("unsloth")) tuning = "LoRA-tuned with Unsloth";
    else if (tags.has("unsloth")) tuning = "Tuned with Unsloth";
    else if (tags.has("lora")) tuning = "LoRA-tuned";
    if (tuning && (tags.has("agent") || tags.has("tool-use"))) tuning += " for agent tool use";
    const sentences = [`${lead}.`];
    if (tuning) sentences.push(`${tuning}.`);
    if (model.pipeline_tag === "image-text-to-text") sentences.push("Takes text and image inputs.");
    if (tags.has("dgx-spark")) {
      sentences.push(quantised ? "Quantised on one DGX Spark." : "Trained on one DGX Spark.");
    }
    return sentences.join(" ");
  };

  const metaLine = (model) => {
    const parts = [format(model)];
    const quants = quantisations(model);
    if (quants.length) parts.push(quants.join(" / "));
    const base = baseModel(model);
    if (base) parts.push(`base ${base}`);
    if (license(model)) parts.push(license(model));
    return parts.join(" · ");
  };

  const cardTags = (model) => {
    const present = new Set(tagList(model));
    const fmt = format(model);
    let out = TAG_LABELS.filter(([tag]) => present.has(tag)).map(([, label]) => label);
    out = out.filter((label) => label !== "Safetensors" || fmt === "Safetensors");
    if (!out.includes(fmt)) out.unshift(fmt);
    return out.slice(0, MAX_TAGS);
  };

  const node = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  };

  const linkTo = (href, text) => {
    const a = node("a", "", text);
    a.href = href;
    a.rel = "noopener";
    return a;
  };

  const card = (model) => {
    const name = model.id.split("/").pop();
    const url = `https://huggingface.co/${model.id}`;
    const cardEl = node("div", "card fade-in");
    cardEl.dataset.hf = name;

    const title = node("h3", "card-title");
    title.appendChild(linkTo(url, name));
    cardEl.appendChild(title);

    const text = description(model);
    if (text) cardEl.appendChild(node("p", "card-desc", text));
    const meta = metaLine(model);
    if (meta) cardEl.appendChild(node("p", "card-meta", meta));

    const tagRow = node("div", "tags");
    cardTags(model).forEach((label) => tagRow.appendChild(node("span", "tag sm", label)));
    cardEl.appendChild(tagRow);

    const links = node("div", "card-links");
    links.appendChild(linkTo(url, "Hugging Face"));
    cardEl.appendChild(links);

    fade.observe(cardEl);
    return cardEl;
  };

  const publicModels = (models) =>
    models.filter(
      (m) => m && typeof m.id === "string" && m.id.startsWith(`${author}/`) && !m.private && !m.gated
    );

  const render = (models) => {
    const committed = new Map();
    for (const child of grid.children) {
      if (child.dataset.hf) committed.set(child.dataset.hf, child);
    }
    const ordered = publicModels(models).map((model) => {
      const name = model.id.split("/").pop();
      return committed.get(name) || card(model);
    });

    grid.textContent = "";
    if (!ordered.length) {
      const empty = node("p", "card-meta");
      empty.textContent = "No public models yet on ";
      empty.appendChild(linkTo(`https://huggingface.co/${author}`, author));
      grid.appendChild(empty);
      return;
    }
    ordered.forEach((el) => grid.appendChild(el));
  };

  const query = FIELDS.map((f) => `&expand[]=${f}`).join("");
  fetch(`${API}?author=${encodeURIComponent(author)}${query}&sort=lastModified&direction=-1`, {
    headers: { Accept: "application/json" },
  })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((models) => {
      if (!Array.isArray(models)) throw new Error("unexpected response");
      render(models);
    })
    .catch((e) => console.warn("models: keeping the committed list, Hugging Face request failed:", e));
})();
