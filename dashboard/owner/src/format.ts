// Indian money format, matching the payload's own strings (present/owner_view.py fmt_rs).

export function groupEnIn(n: number): string {
  return Math.round(n).toLocaleString("en-IN");
}

function oneDecimal(x: number): string {
  const s = x.toFixed(1);
  return s.endsWith(".0") ? s.slice(0, -2) : s;
}

export function rs(paise: number): string {
  const r = Math.abs(paise) / 100;
  const body = r >= 1e7 ? `Rs ${oneDecimal(r / 1e7)} crore` : r >= 1e5 ? `Rs ${oneDecimal(r / 1e5)} lakh` : `Rs ${groupEnIn(r)}`;
  return (paise < 0 ? "minus " : "") + body;
}

// Short axis ticks: "Rs 20L", "Rs 1.2Cr", "-Rs 5L".
export function rsTick(paise: number): string {
  const r = paise / 100;
  const a = Math.abs(r);
  const s = a >= 1e7 ? `${oneDecimal(a / 1e7)} Cr` : a >= 1e5 ? `${oneDecimal(a / 1e5)} L` : groupEnIn(a);
  return `${r < 0 ? "-" : ""}Rs ${s}`;
}

export function pct(p: number): string {
  if (p <= 0) return "0%";
  if (p < 0.005) return "under 1%";
  return `${Math.min(Math.round(p * 100), 100)}%`;
}

export function shortDate(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Partial<HTMLElementTagNameMap[K]> & { cls?: string } = {},
  ...children: (Node | string | null | undefined)[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  const { cls, ...rest } = props;
  if (cls) e.className = cls;
  Object.assign(e, rest);
  for (const c of children) if (c != null) e.append(c);
  return e;
}
