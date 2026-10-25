// Automations for the presentation: what each action button drafts and which notifications it simulates.

import { rs, shortDate } from "./format";
import { MSME_STRETCH_CATCH, MSMED_MAX_DAYS, type BuyerRow, type ObligationRow, type TaskAction } from "./viewmodel";

export const DEMO_NOTE = "Demo: nothing is sent. Messages and filings are simulated for the presentation.";

export const ACTION_LABELS = {
  remind: "Remind",
  remindAll: "Remind all overdue",
  notice: "MSMED notice",
  doThis: "Do this",
  remindMe: "Remind me",
  notifications: "Notifications",
  noNotifications: "Nothing yet. Actions you take show up here.",
  demoPill: "Demo actions",
  cancel: "Cancel",
};

export interface Channel { id: string; label: string }
export const REMINDER_CHANNELS: Channel[] = [
  { id: "whatsapp", label: "WhatsApp" }, { id: "email", label: "Email" }, { id: "sms", label: "SMS" },
];

/** One simulated notification: shown `afterMs` after the action is confirmed. */
export interface DemoEvent { afterMs: number; title: string; body: string }

/** What a button opens: a dialog with an editable draft, and what confirming it simulates. */
export interface ActionSpec {
  title: string;
  subtitle: string | null;
  draft: string | null;
  channels: Channel[] | null;
  notes: { text: string; serious: boolean }[];
  confirm: string;
  /** Events for the chosen channel (null when the dialog has no channel choice). */
  events: (channel: Channel | null) => DemoEvent[];
}

const SIGN_OFF = "[Your business name]";
const ref = (prefix: string, seed: string) => {
  let h = 0;
  for (const c of seed) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `DEMO-${prefix}-${String(h % 1_000_000).padStart(6, "0")}`;
};
const bills = (n: number) => `${n} bill${n === 1 ? "" : "s"}`;

// ---------------------------------------------------------------- customers

export function reminderDraft(r: BuyerRow): string {
  const oldest = r.owedBills[0]?.daysLate;
  const lines = [
    `Dear ${r.buyer} team,`,
    "",
    r.overduePaise > 0
      ? `A friendly reminder that ${r.owedBills.length ? bills(r.owedBills.length) + " totalling " : ""}${rs(r.overduePaise)} ` +
        `${r.owedBills.length === 1 ? "is" : "are"} now past the due date${oldest ? `, the oldest by ${oldest} days` : ""}.`
      : `A friendly reminder about the ${rs(r.stakePaise)} outstanding on your account.`,
  ];
  if (r.interestPaise > 0) {
    lines.push(`As a registered micro or small enterprise, late payments to us carry interest under the MSMED Act; ${rs(r.interestPaise)} has built up so far.`);
  }
  lines.push("Could you let us know when we can expect the payment?", "", "Thank you,", SIGN_OFF);
  return lines.join("\n");
}

export function reminderSpec(r: BuyerRow): ActionSpec {
  return {
    title: `Send a payment reminder to ${r.buyer}`,
    subtitle: r.overduePaise > 0 ? `${rs(r.overduePaise)} overdue` : `${rs(r.stakePaise)} outstanding`,
    draft: reminderDraft(r),
    channels: REMINDER_CHANNELS,
    notes: [{ text: DEMO_NOTE, serious: false }],
    confirm: "Send reminder",
    events: (ch) => {
      const via = ch?.label ?? "message";
      return [
        { afterMs: 0, title: `Reminder sent to ${r.buyer}`, body: `Sent on ${via}.` },
        { afterMs: 2200, title: `Delivered to ${r.buyer}`, body: `${via} shows it as delivered.` },
        { afterMs: 5200, title: `${r.buyer} read your reminder`, body: "Simulated read receipt. Follow up in two days if there is no reply." },
      ];
    },
  };
}

export function remindAllSpec(rows: BuyerRow[]): ActionSpec | null {
  const due = rows.filter((r) => r.overduePaise > 0);
  if (!due.length) return null;
  const total = due.reduce((s, r) => s + r.overduePaise, 0);
  return {
    title: `Send reminders to ${due.length} customers`,
    subtitle: `${rs(total)} overdue in total`,
    draft: null,
    channels: REMINDER_CHANNELS,
    notes: [
      { text: `Each customer gets their own message with their own figures: ${due.map((r) => r.buyer).join(", ")}.`, serious: false },
      { text: DEMO_NOTE, serious: false },
    ],
    confirm: `Send ${due.length} reminders`,
    events: (ch) => [
      { afterMs: 0, title: `${due.length} reminders sent`, body: `Sent on ${ch?.label ?? "message"} for ${rs(total)} overdue.` },
      ...due.slice(0, 3).map((r, i) => ({ afterMs: 2500 + i * 1500, title: `Delivered to ${r.buyer}`, body: `${rs(r.overduePaise)} overdue.` })),
    ],
  };
}

export function msmedNoticeDraft(r: BuyerRow, asOf: string | null): string {
  const principal = r.owedBills.reduce((s, b) => s + b.principalPaise, 0);
  const lines = [
    "Subject: Notice of delayed payment under the MSMED Act, 2006",
    "",
    `To: ${r.buyer}`,
    "",
    "We are a micro / small enterprise registered on Udyam ([Udyam registration number]).",
    `Under Section 15 of the MSMED Act, 2006, payment for goods or services supplied must be made within the agreed period, which cannot exceed ${MSMED_MAX_DAYS} days from acceptance.`,
    "",
    "These bills remain unpaid past that limit:",
    ...r.owedBills.map((b) => `  • Bill of ${rs(b.principalPaise)}: ${b.daysLate} days late, interest ${rs(b.interestPaise)}`),
    "",
    `Total unpaid: ${rs(principal)}. Interest built up${asOf ? ` to ${shortDate(asOf)}` : ""}: ${rs(r.interestPaise)}.`,
    "Under Section 16, interest on delayed payment is compound interest with monthly rests at three times the bank rate notified by the Reserve Bank of India.",
    "",
    "We request payment of the amount due and the interest. If it remains unpaid, we may refer the matter to the Micro and Small Enterprises Facilitation Council under Section 18 through the MSME Samadhaan portal.",
    "",
    SIGN_OFF,
  ];
  return lines.join("\n");
}

export function msmedNoticeSpec(r: BuyerRow, asOf: string | null): ActionSpec | null {
  if (!r.owedBills.length || r.interestPaise <= 0) return null;
  const principal = r.owedBills.reduce((s, b) => s + b.principalPaise, 0);
  return {
    title: `MSMED notice to ${r.buyer}`,
    subtitle: `${bills(r.owedBills.length)}, ${rs(principal)} unpaid, ${rs(r.interestPaise)} interest`,
    draft: msmedNoticeDraft(r, asOf),
    channels: null,
    notes: [
      { text: "A draft to check before sending, not legal advice. Fill in the bracketed details.", serious: false },
      { text: "A formal notice can strain the relationship; consider sending a reminder first.", serious: true },
      { text: DEMO_NOTE, serious: false },
    ],
    confirm: "Send notice and file on Samadhaan",
    events: () => {
      const id = ref("SAM", r.buyer + r.interestPaise);
      return [
        { afterMs: 0, title: `Notice sent to ${r.buyer}`, body: "Emailed as a PDF with the bill details." },
        { afterMs: 2600, title: "Filed on MSME Samadhaan", body: `Reference ${id}. Claim of ${rs(principal)} plus ${rs(r.interestPaise)} interest.` },
        { afterMs: 6000, title: "Acknowledgement received", body: `The Facilitation Council decides within 90 days of taking up the reference. Reference ${id}.` },
      ];
    },
  };
}

// ---------------------------------------------------------------- plan steps

export function supplierDraft(supplier: string, days: number | null): string {
  return [
    `Dear ${supplier},`,
    "",
    `Could we pay your next invoices ${days ? `${days} days` : "a little"} later than our usual terms, this one time?`,
    "We value working with you and will keep you updated on the dates.",
    "",
    "Thank you,",
    SIGN_OFF,
  ].join("\n");
}

export function taskSpec(a: TaskAction, rows: BuyerRow[]): ActionSpec | null {
  switch (a.kind) {
    case "remind": {
      const r = rows.find((x) => x.buyer === a.buyer);
      return r ? reminderSpec(r) : null;
    }
    case "supplier":
      return {
        title: `Ask ${a.supplier} for more time`,
        subtitle: a.days ? `${a.days} more days` : null,
        draft: supplierDraft(a.supplier, a.days),
        channels: REMINDER_CHANNELS,
        notes: [...(a.msme ? [{ text: MSME_STRETCH_CATCH, serious: true }] : []), { text: DEMO_NOTE, serious: false }],
        confirm: "Send request",
        events: (ch) => [
          { afterMs: 0, title: `Request sent to ${a.supplier}`, body: `Sent on ${ch?.label ?? "message"}.` },
          { afterMs: 4000, title: `${a.supplier} replied`, body: "Simulated reply: \"Let us check and confirm by tomorrow.\"" },
        ],
      };
    case "treds": {
      const bill = a.billPaise, fee = a.feePaise;
      const get = bill !== null && fee !== null ? bill - fee : null;
      return {
        title: "Get early cash through TReDS",
        subtitle: [bill !== null ? `Bill of ${rs(bill)}` : null, a.buyer].filter(Boolean).join(" to ") || null,
        draft: null,
        channels: null,
        notes: [
          { text: `The bill is listed on a TReDS platform; financiers bid, and you get the money within days${get !== null ? `: about ${rs(get)} after a fee of about ${rs(fee as number)}` : ""}.`, serious: false },
          { text: DEMO_NOTE, serious: false },
        ],
        confirm: "List the bill on TReDS",
        events: () => [
          { afterMs: 0, title: "Bill listed on TReDS", body: bill !== null ? `Bill of ${rs(bill)}${a.buyer ? ` to ${a.buyer}` : ""}.` : "Waiting for bids." },
          { afterMs: 3500, title: "Financier bid received", body: get !== null ? `You would get about ${rs(get)} (fee about ${rs(fee as number)}).` : "Review the bid." },
        ],
      };
    }
    case "overdraft":
      return {
        title: "Request an overdraft draw",
        subtitle: [a.amountPaise !== null ? rs(a.amountPaise) : null, a.date ? `on ${shortDate(a.date)}` : null].filter(Boolean).join(" ") || null,
        draft: null,
        channels: null,
        notes: [{ text: "A draw request goes to your bank for the date in the plan.", serious: false }, { text: DEMO_NOTE, serious: false }],
        confirm: "Send draw request",
        events: () => [
          { afterMs: 0, title: "Draw request sent to your bank", body: `${a.amountPaise !== null ? rs(a.amountPaise) : "Amount from the plan"}${a.date ? ` on ${shortDate(a.date)}` : ""}.` },
          { afterMs: 3000, title: "Bank acknowledged the request", body: "Simulated acknowledgement." },
        ],
      };
  }
}

// ---------------------------------------------------------------- obligations

/** "Remind me" on a payment: two days before its due date (or on it, when that is sooner than today allows). */
export function obligationReminderEvents(r: ObligationRow, asOf: string | null): DemoEvent[] {
  const due = new Date(r.date + "T00:00:00");
  const at = new Date(due);
  at.setDate(at.getDate() - 2);
  const start = asOf ? new Date(asOf + "T00:00:00") : null;
  const when = start && at <= start ? due : at;
  const iso = `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, "0")}-${String(when.getDate()).padStart(2, "0")}`;
  return [{ afterMs: 0, title: `Reminder set: ${r.label}`, body: `We will remind you on ${shortDate(iso)} to pay ${r.amount} due ${r.dateLabel}.` }];
}
