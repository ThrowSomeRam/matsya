// Demo action UI: the draft dialog, toasts and the notification bell. Layout only; the specs (titles, drafts,
// notes, simulated events) come from automations.ts.

import { ACTION_LABELS, type ActionSpec, type Channel, type DemoEvent } from "./automations";
import { el } from "./format";

interface Note extends DemoEvent { at: Date }

const log: Note[] = [];
let unread = 0;
const toasts = el("div", { cls: "toasts" });
toasts.setAttribute("aria-live", "polite");
const count = el("span", { cls: "bell-count" });
const panel = el("div", { cls: "bell-panel" });
panel.hidden = true;

function since(d: Date): string {
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  return s < 10 ? "just now" : s < 60 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

function renderPanel() {
  panel.replaceChildren(
    el("p", { cls: "bell-title" }, ACTION_LABELS.notifications),
    log.length
      ? el("ul", {}, ...[...log].reverse().map((n) => el("li", {},
        el("p", { cls: "n-title" }, el("span", { cls: "demo-tag" }, "Demo"), n.title),
        el("p", { cls: "n-body" }, n.body),
        el("p", { cls: "n-time" }, since(n.at)))))
      : el("p", { cls: "n-empty" }, ACTION_LABELS.noNotifications));
  count.textContent = unread ? String(unread) : "";
  count.hidden = !unread;
}

function notify(e: DemoEvent) {
  log.push({ ...e, at: new Date() });
  if (panel.hidden) unread++;
  renderPanel();
  const t = el("div", { cls: "toast" }, el("p", { cls: "n-title" }, el("span", { cls: "demo-tag" }, "Demo"), e.title), el("p", { cls: "n-body" }, e.body));
  toasts.append(t);
  setTimeout(() => t.classList.add("out"), 4200);
  setTimeout(() => t.remove(), 4600);
}

export function play(events: DemoEvent[]) {
  for (const e of events) setTimeout(() => notify(e), e.afterMs);
}

/** The bell for the app bar, and the toast stack (appended to the body once). */
export function bell(): HTMLElement {
  document.body.append(toasts);
  const btn = el("button", { type: "button", cls: "bell" });
  btn.setAttribute("aria-label", ACTION_LABELS.notifications);
  btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" class="icon"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';
  btn.append(count);
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    panel.hidden = !panel.hidden;
    if (!panel.hidden) unread = 0;
    renderPanel();
  });
  document.addEventListener("click", (ev) => { if (!panel.contains(ev.target as Node)) panel.hidden = true; });
  setInterval(() => { if (!panel.hidden) renderPanel(); }, 15000);
  renderPanel();
  return el("div", { cls: "bell-wrap" }, el("span", { cls: "demo-pill" }, ACTION_LABELS.demoPill), btn, panel);
}

/** Open the draft dialog for a spec; confirming plays its simulated events. */
export function openAction(spec: ActionSpec) {
  const dlg = el("dialog", { cls: "action-dialog" });
  let channel: Channel | null = spec.channels?.[0] ?? null;
  const chips = (spec.channels ?? []).map((c) => {
    const b = el("button", { type: "button", cls: "chip" }, c.label);
    b.addEventListener("click", () => { channel = c; chips.forEach((x, i) => x.setAttribute("aria-pressed", String(spec.channels![i] === c))); });
    b.setAttribute("aria-pressed", String(c === channel));
    return b;
  });
  const text = spec.draft !== null ? el("textarea", { cls: "draft", value: spec.draft, rows: Math.min(18, spec.draft.split("\n").length + 1) }) : null;
  const close = () => { dlg.close(); dlg.remove(); };
  const ok = el("button", { type: "button", cls: "btn primary" }, spec.confirm);
  ok.addEventListener("click", () => { close(); play(spec.events(channel)); });
  const cancel = el("button", { type: "button", cls: "btn" }, ACTION_LABELS.cancel);
  cancel.addEventListener("click", close);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  dlg.append(
    el("h2", { cls: "card-title" }, spec.title),
    spec.subtitle ? el("p", { cls: "card-sub" }, spec.subtitle) : el("span", {}),
    chips.length ? el("div", { cls: "chips" }, ...chips) : el("span", {}),
    text ?? el("span", {}),
    ...spec.notes.map((n) => {
      const p = el("p", { cls: "action-note" }, n.text);
      p.dataset.serious = String(n.serious);
      return p;
    }),
    el("div", { cls: "dialog-actions" }, cancel, ok));
  document.body.append(dlg);
  dlg.showModal();
  (text ?? ok).focus();
  if (text) {
    text.setSelectionRange(0, 0);
    text.scrollTop = 0;
  }
}
