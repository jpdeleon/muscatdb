// Canonical numeric display formatting for the GUI. Consolidates the
// precision-aware formatters that used to be copy-pasted per page
// (toi.html/nexsci.html fmtNum, ephemeris.html formatSummaryNumber,
// transit_fit.html fmtSig/fmtDp).
//
// Project display rule: print values to at most MAX_DECIMALS decimals when
// no domain-specific precision is known (see auto()). Callers that already
// know the right precision for their quantity (e.g. BJD_TDB timestamps
// needing 8 decimals, or fit parameters with a chosen sig-fig count) pass
// their own `dp`/`sf` through fixed()/sigFigs() unclamped, since scientific
// exports must not be silently truncated by a generic display default.
window.MuscatFormat = (function () {
  var MAX_DECIMALS = 6;

  // Round to `sf` significant figures. Non-finite or empty input falls back
  // to the original trimmed string so callers can still display raw text.
  function sigFigs(x, sf) {
    if (x === undefined || x === null) return "";
    var s = String(x).trim();
    if (s === "") return "";
    var n = Number(s);
    if (isNaN(n) || !isFinite(n)) return s;
    if (n === 0) return "0";
    return String(Number(n.toPrecision(sf)));
  }

  // Round to a fixed number of decimal places, trimmed to a clean number.
  // Non-finite or empty input falls back to the original trimmed string.
  function fixed(x, dp) {
    if (x === undefined || x === null) return "";
    var s = String(x).trim();
    if (s === "") return "";
    var n = Number(s);
    if (isNaN(n) || !isFinite(n)) return s;
    return String(Number(n.toFixed(dp)));
  }

  // Fixed-point formatting for numeric-only inputs (e.g. CSV/text exports):
  // returns '' for anything non-finite instead of echoing raw text back.
  function fixedOrBlank(value, dp) {
    var n = Number(value);
    if (!isFinite(n)) return "";
    return n.toFixed(dp).replace(/0+$/, "").replace(/\.$/, "");
  }

  // Magnitude-bucketed default display: whole numbers as-is, then
  // progressively fewer decimals as magnitude grows, never exceeding
  // MAX_DECIMALS. Use this when a value has no known domain-specific
  // precision (e.g. a generic catalog column).
  function auto(value) {
    if (value === null || value === undefined || value === "") return "—";
    var x = Number(value);
    if (!isFinite(x)) return "—";
    if (Number.isInteger(x)) return String(x);
    var a = Math.abs(x);
    var s;
    if (a >= 1000) s = x.toFixed(0);
    else if (a >= 100) s = x.toFixed(1);
    else if (a >= 1) s = x.toFixed(3);
    else s = x.toPrecision(3);
    return String(parseFloat(s));
  }

  return {
    MAX_DECIMALS: MAX_DECIMALS,
    sigFigs: sigFigs,
    fixed: fixed,
    fixedOrBlank: fixedOrBlank,
    auto: auto,
  };
})();
