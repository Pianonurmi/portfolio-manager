import { describe, expect, it } from "vitest";
import { compact, money, pct, quantity, signedMoney } from "./format";

const clean = (s: string) => s.replace(/[  ]/g, " ");

describe("fi-FI formatting", () => {
  it("formats money with space grouping and comma decimals", () => {
    expect(clean(money("1234.5", "EUR", 2))).toBe("1 234,50 €");
    expect(clean(money(-12))).toBe("−12 €");
  });
  it("signs deltas", () => {
    expect(clean(signedMoney(-46212))).toBe("−46 212 €");
    expect(clean(signedMoney(5))).toBe("+5 €");
  });
  it("formats quantities", () => {
    expect(clean(quantity("520"))).toBe("520");
    expect(clean(quantity("0.034"))).toBe("0,034");
  });
  it("formats percentages and compact values", () => {
    expect(clean(pct(0.142))).toBe("14,2 %");
    expect(clean(compact(600000))).toBe("600 k€");
  });
  it("never shows NaN", () => {
    expect(money("abc")).toBe("—");
  });
});
