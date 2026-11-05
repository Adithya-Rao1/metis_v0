// Project notes (private to the owner): list, add, edit, delete with a confirmation. Every string from the server goes in via textContent.
import { el, say, stateBox, confirmDialog, fmtDate } from "./chrome.js";
import { authed, del, describeError, get, patch, post } from "./api.js";

export async function mountNotes(host, pid) {
  let max = 5000;
  const list = el("div", { class: "notes-list", "data-testid": "notes-list" });
  const input = el("textarea", { id: "note-new", class: "note-input", rows: "3", maxlength: String(max), "aria-describedby": "note-help" });
  const addBtn = el("button", { class: "btn primary", type: "submit", id: "note-add", text: "Add note" });
  const msg = el("span", { class: "legend", id: "note-msg", role: "status", "aria-live": "polite" });
  const form = el("form", { id: "note-form", class: "note-form", method: "post", action: "#js-required", novalidate: true },
    el("div", { class: "field" }, el("label", { for: "note-new", text: "Add a note" }), input,
      el("span", { class: "legend", id: "note-help", text: "Only you can see notes. They belong to this project, not to one compound." })),
    el("div", { class: "inline" }, addBtn, msg));
  host.replaceChildren(el("h2", { id: "notes-h", text: "Notes" }), list, form);

  async function load() {
    try {
      const r = await authed(() => get(`/projects/${pid}/notes`));
      max = r.max_chars || max; input.maxLength = max;
      render(r.notes);
    } catch (e) { list.replaceChildren(stateBox("error", describeError(e).message, { retry: load })); }
  }

  function render(notes) {
    if (!notes.length) { list.replaceChildren(el("p", { class: "legend", id: "notes-empty", text: "No notes yet." })); return; }
    list.replaceChildren(...notes.map((n, i) => row(n, i)));
  }

  function row(n, i) {
    const text = el("p", { class: "note-text", "data-note-text": "", text: n.body });
    const when = el("span", { class: "legend", text: fmtDate(n.created_at) });
    const edit = el("button", { class: "btn small", type: "button", "aria-label": `Edit note ${i + 1}`, text: "Edit" });
    const remove = el("button", { class: "btn small", type: "button", "aria-label": `Delete note ${i + 1}`, text: "Delete" });
    const box = el("div", { class: "note-row", "data-note": n.id }, text, el("div", { class: "inline" }, when, edit, remove));
    edit.addEventListener("click", () => {
      const ta = el("textarea", { class: "note-input", rows: "3", maxlength: String(max), "aria-label": `Edit note ${i + 1}`, id: `note-edit-${i}` });
      ta.value = n.body;
      const save = el("button", { class: "btn primary small", type: "submit", text: "Save" });
      const cancel = el("button", { class: "btn small", type: "button", text: "Cancel" });
      const m = el("span", { class: "legend", role: "status", "aria-live": "polite" });
      const f = el("form", { class: "note-edit", method: "post", action: "#js-required", novalidate: true }, ta, el("div", { class: "inline mt6" }, save, cancel, m));
      cancel.addEventListener("click", () => box.replaceWith(row(n, i)));
      f.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const body = ta.value.trim();
        if (!body) { say(m, "A note cannot be empty. Delete it instead.", "error"); return; }
        save.disabled = true; say(m, "Saving...", "ok");
        try { const u = await authed(() => patch(`/projects/${pid}/notes/${n.id}`, { body })); box.replaceWith(row(u, i)); }
        catch (e) { save.disabled = false; say(m, describeError(e).message, "error"); }
      });
      box.replaceChildren(f);
      ta.focus();
    });
    remove.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "Delete note", body: "Delete this note? This cannot be undone.", okLabel: "Delete note" });
      if (!ok) { remove.focus(); return; }
      try { await authed(() => del(`/projects/${pid}/notes/${n.id}`)); await load(); say(msg, "Note deleted.", "ok"); input.focus(); }
      catch (e) { say(msg, describeError(e).message, "error"); }
    });
    return box;
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = input.value.trim();
    if (!body) { say(msg, "Write something first.", "error"); input.focus(); return; }
    addBtn.disabled = true; say(msg, "Saving...", "ok");
    try { await authed(() => post(`/projects/${pid}/notes`, { body })); input.value = ""; await load(); say(msg, "Note added.", "ok"); }
    catch (e) { say(msg, describeError(e).message, "error"); }
    finally { addBtn.disabled = false; }
  });
  await load();
}
