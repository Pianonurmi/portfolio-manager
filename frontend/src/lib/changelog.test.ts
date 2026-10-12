import { describe, expect, it } from "vitest";
import pkg from "../../package.json";
import { parseChangelog, RELEASES, VERSION } from "./changelog";

describe("changelog", () => {
  it("reads releases, dates, summaries and sections", () => {
    const r = parseChangelog(
      "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n### Added\n- Next thing\n\n## [1.2.0] - 2026-11-01\n\nA summary\nover two lines.\n\n### Added\n- One\n- Two\n### Fixed\n- Three\n",
    );
    expect(r.map((x) => [x.version, x.date])).toEqual([["Unreleased", null], ["1.2.0", "2026-11-01"]]);
    expect(r[1]!.summary).toBe("A summary over two lines.");
    expect(r[1]!.sections).toEqual([{ title: "Added", items: ["One", "Two"] }, { title: "Fixed", items: ["Three"] }]);
  });
  it("drops an empty Unreleased section", () => {
    expect(parseChangelog("## [Unreleased]\n\n## [1.0.0] - 2026-10-11\n\n### Added\n- X\n").map((x) => x.version)).toEqual(["1.0.0"]);
  });
  it("matches the version in package.json (bump both when releasing)", () => {
    expect(RELEASES.length).toBeGreaterThan(0);
    expect(VERSION).toBe(pkg.version);
  });
});
