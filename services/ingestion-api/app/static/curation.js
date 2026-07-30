// Issue #266: shared between curate.html (the pending_review queue) and
// curate_list.html (the master list, any status). Both render curator-facing
// document fields -- filename, source_originator, uploader-supplied evidence
// strings -- that are attacker-controlled input, not markup. See issue #207:
// this app's queue page used to build rows with innerHTML/template literals,
// which made an uploader-chosen filename executable script in a curator's
// session. Every helper here builds DOM nodes and assigns values through
// .textContent/.value instead, so a value can never be parsed as markup no
// matter which field it arrives in -- adding a new field is safe by default.

function el(tag, opts = {}) {
  const node = document.createElement(tag);
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.className) node.className = opts.className;
  if (opts.id) node.id = opts.id;
  for (const [k, v] of Object.entries(opts.attrs || {})) node.setAttribute(k, v);
  for (const child of opts.children || []) node.append(child);
  return node;
}

function optionNodes(values, selected) {
  // Guard against a value that's since been retired from the
  // admin-configurable list (C9) -- without this, the <select> would
  // silently default to its first option instead of the document's actual
  // value, and an inattentive save would then silently *change* it.
  const all = values.includes(selected) ? values : [selected, ...values];
  return all.map((v) => {
    const opt = el("option", { text: v });
    opt.value = v;
    opt.selected = v === selected;
    return opt;
  });
}

function multiOptionNodes(values, selectedValues) {
  // Same retired-value guard as optionNodes above, but for a <select
  // multiple> whose current value is a list (Releasability, FR-20/Section
  // 6.3) rather than a single string.
  const missing = selectedValues.filter((v) => !values.includes(v));
  return [...values, ...missing].map((v) => {
    const opt = el("option", { text: v });
    opt.value = v;
    opt.selected = selectedValues.includes(v);
    return opt;
  });
}

// Issue #266: human-readable feedback instead of a raw JSON dump. FastAPI's
// default error body is `{"detail": "..."}` for a plain HTTPException, or
// `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` for a pydantic
// validation error -- both are unwrapped into prose here rather than shown to
// the curator as JSON they have to parse themselves.
function errorMessage(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg) || JSON.stringify(d)).join("; ");
  }
  return `Request failed (HTTP ${status}).`;
}

async function reportResult(target, res, successText) {
  const body = await res.json().catch(() => null);
  target.hidden = false;
  target.className = "msg " + (res.ok ? "ok" : "err");
  target.textContent = res.ok
    ? typeof successText === "function"
      ? successText(body)
      : successText
    : errorMessage(body, res.status);
  return body;
}
