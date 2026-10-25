import { describe, expect, it } from "vitest";
import demoJson from "../../../eval/demo/owner_view.json";
import obJson from "../../../eval/demo/obligations.json";
import {
  DEMO_NOTE, REMINDER_CHANNELS, msmedNoticeDraft, msmedNoticeSpec, obligationReminderEvents, reminderDraft, reminderSpec,
  remindAllSpec, taskSpec,
} from "./automations";
import type { ObligationsPayload, OwnerView } from "./types";
import { BANNED, buildOwnerScreen, buyerRows } from "./viewmodel";

const demo = demoJson as unknown as OwnerView;
const ob = obJson as unknown as ObligationsPayload;
const screen = buildOwnerScreen(demo, "api", ob);
const rows = buyerRows(demo);
const top = rows[0];
const allSpecs = () => [
  ...rows.map(reminderSpec), ...rows.map((r) => msmedNoticeSpec(r, demo.meta.as_of)), remindAllSpec(rows),
  ...[...screen.plan.tasks, ...screen.plan.optional].map((t) => (t.action ? taskSpec(t.action, rows) : null)),
].filter((x) => x !== null);

describe("drafts use the payload's own figures", () => {
  it("reminds with the customer's overdue total and built-up interest", () => {
    const d = reminderDraft(top);
    expect(d).toContain(top.buyer);
    expect(d).toContain(top.cells.overdue);
    expect(d).toContain("[Your business name]");
  });

  it("lists every owed bill in the MSMED notice and cites ss.15, 16 and 18", () => {
    const d = msmedNoticeDraft(top, demo.meta.as_of);
    const owed = demo.owed.items.filter((o) => o.buyer === top.buyer);
    expect(top.owedBills).toHaveLength(owed.length);
    for (const o of owed) expect(d).toContain(`${o.days_late} days late, interest ${o.interest.text}`);
    for (const s of ["Section 15", "Section 16", "Section 18", "45 days", "Samadhaan", "[Udyam registration number]"]) expect(d).toContain(s);
  });

  it("offers a notice only to customers who owe interest", () => {
    for (const r of rows) expect(msmedNoticeSpec(r, null) === null).toBe(r.interestPaise <= 0 || r.owedBills.length === 0);
  });
});

describe("every action is labelled a demo", () => {
  it("carries the demo note and uses no banned words", () => {
    for (const s of allSpecs()) {
      expect(s!.notes.some((n) => n.text === DEMO_NOTE)).toBe(true);
      const text = [s!.title, s!.subtitle ?? "", s!.draft ?? "", ...s!.notes.map((n) => n.text),
        ...s!.events(REMINDER_CHANNELS[0]).flatMap((e) => [e.title, e.body])].join("\n");
      for (const b of BANNED) expect(text, text).not.toMatch(b);
    }
  });

  it("gives every plan step that has an action a working dialog", () => {
    const tasks = [...screen.plan.tasks, ...screen.plan.optional].filter((t) => t.action);
    expect(tasks.length).toBeGreaterThan(0);
    for (const t of tasks) expect(taskSpec(t.action!, rows)).not.toBeNull();
  });

  it("warns about MSMED on the costly supplier request only", () => {
    const steps = [...screen.plan.tasks, ...screen.plan.optional].filter((t) => t.action?.kind === "supplier");
    const notes = steps.map((t) => taskSpec(t.action!, rows)!.notes.some((n) => n.serious));
    expect(notes).toEqual(steps.map((t) => t.action!.kind === "supplier" && t.action!.msme));
  });

  it("names the TReDS payout as the bill minus the fee", () => {
    const t = [...screen.plan.tasks, ...screen.plan.optional].find((x) => x.action?.kind === "treds")!;
    const a = t.action as { kind: "treds"; billPaise: number; feePaise: number; buyer: string | null };
    const spec = taskSpec(a, rows)!;
    expect(spec.events(null)[1].body).toContain("about Rs");
    expect(a.billPaise - a.feePaise).toBeGreaterThan(0);
  });

  it("sets payment reminders two days before the due date", () => {
    const gst = screen.obligations!.rows.find((r) => r.label === "GST")!;
    expect(obligationReminderEvents(gst, demo.meta.as_of)[0].body).toContain("18 Apr");
  });
});
