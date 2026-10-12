// What's new: every release and what it changed, from CHANGELOG.md.
import { Chip, Panel, ScreenHead } from "../components/ui";
import { RELEASES, VERSION } from "../lib/changelog";
import { date } from "../lib/format";

export function WhatsNew() {
  return (
    <>
      <ScreenHead title="What's new" sub={`You are running version ${VERSION}`} />
      <div className="grid" style={{ gap: 12 }}>
        {RELEASES.map((r) => (
          <Panel
            key={r.version}
            title={r.date ? `Version ${r.version}` : "Not yet released"}
            meta={
              r.date ? (
                <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                  {r.version === VERSION && <Chip tone="info">current</Chip>}
                  {date(r.date)}
                </span>
              ) : (
                "already running here; part of the next version"
              )
            }
          >
            {r.summary && <p style={{ marginTop: 0 }}>{r.summary}</p>}
            {r.sections.map((s) => (
              <div key={s.title} style={{ marginBottom: 10 }}>
                <h4 style={{ margin: "6px 0 4px", fontSize: 13, color: "var(--fg-2)" }}>{s.title}</h4>
                <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.5, listStyle: "disc" }}>
                  {s.items.map((it, i) => <li key={i}>{it}</li>)}
                </ul>
              </div>
            ))}
          </Panel>
        ))}
      </div>
    </>
  );
}
