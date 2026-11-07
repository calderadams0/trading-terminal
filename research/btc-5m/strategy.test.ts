import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { validateStrategySafety } from "../../lib/freqtrade-guard";

// The research strategy must survive the same validator every generated strategy does.
// Passing means "safe to run", not "profitable" — see research/btc-5m/README.md.
describe("research/btc-5m strategy", () => {
  it("Btc5mReversion passes lib/freqtrade-guard", () => {
    const code = readFileSync(join(__dirname, "strategies/Btc5mReversion.py"), "utf8");
    const errors = validateStrategySafety({
      config: { stoploss: -0.01, minimal_roi: { "0": 0.01 } },
      code,
      leverage: 1,
    });
    expect(errors).toEqual([]);
  });
});
