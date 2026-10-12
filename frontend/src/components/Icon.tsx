const P: Record<string, string> = {
  household: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9v11h14V9"/><path d="M14 13.5a2.5 2.5 0 1 1-3.5 2.3L8 18.3"/>',
  home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  portfolio: '<path d="M3 3v18h18"/><path d="m7 15 4-5 3 3 5-7"/>',
  networth: '<path d="M12 3v18M5 7h14M7 7l-3 7a3 3 0 0 0 6 0zM17 7l-3 7a3 3 0 0 0 6 0z"/>',
  imports: '<path d="M12 3v12m0 0-4-4m4 4 4-4"/><path d="M4 17v3h16v-3"/>',
  plan: '<path d="M3 20c4-1 6-4 9-8s5-6 9-7"/><path d="M3 20c4-3 7-5 10-6s5-2 8-2" opacity=".5"/>',
  kids: '<circle cx="9" cy="8" r="3"/><circle cx="17" cy="10" r="2.2"/><path d="M3 20c.5-3.5 3-5.5 6-5.5s5.5 2 6 5.5M14.5 20c.3-2.3 1.5-3.8 3.5-3.8S21 17.7 21 20"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  cashflow: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  tax: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  theme: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  keys: '<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  logout: '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3"/>',
  release: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>',
};

export function Icon({ name, size }: { name: string; size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      width={size}
      height={size}
      style={size ? { stroke: "currentColor", fill: "none", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" } : undefined}
      dangerouslySetInnerHTML={{ __html: P[name] ?? "" }}
    />
  );
}
