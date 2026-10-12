// Release notes come from CHANGELOG.md at the repo root (Keep a Changelog), bundled at build time.
import text from "../../../CHANGELOG.md?raw";

export type Section = { title: string; items: string[] };
export type Release = { version: string; date: string | null; summary: string; sections: Section[] };

export function parseChangelog(md: string): Release[] {
  const out: Release[] = [];
  let rel: Release | null = null;
  let sec: Section | null = null;
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const head = /^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?/.exec(line);
    if (head) {
      rel = { version: head[1]!, date: head[2] ?? null, summary: "", sections: [] };
      sec = null;
      out.push(rel);
    } else if (rel && line.startsWith("### ")) {
      sec = { title: line.slice(4).trim(), items: [] };
      rel.sections.push(sec);
    } else if (rel && sec && line.startsWith("- ")) {
      sec.items.push(line.slice(2).trim());
    } else if (rel && !sec && line.trim()) {
      rel.summary = rel.summary ? `${rel.summary} ${line.trim()}` : line.trim();
    }
  }
  return out.filter((r) => r.sections.some((s) => s.items.length) || r.summary);
}

export const RELEASES = parseChangelog(text);
// the newest dated release; [Unreleased] entries are deployed from main but not yet tagged
export const VERSION = RELEASES.find((r) => r.date)?.version ?? "0.0.0";
