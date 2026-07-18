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

  // Base-10 exponent of `x`, read off its exponential-notation string
  // instead of Math.log10() to avoid float rounding (log10(0.1) can come
  // out as -0.9999999999999998, which would silently pick the wrong
  // decimal-place bucket below).
  function orderOfMagnitude(x) {
    var exp = Math.abs(x).toExponential().split("e")[1];
    return parseInt(exp, 10);
  }

  // Format a value alongside its uncertainty using the standard scientific
  // convention: round the uncertainty to `sf` significant figures (1 by
  // default), then display the value to that same decimal place so the two
  // numbers stay consistent (e.g. period 3.14159 ± 0.002 -> "3.142 ± 0.002").
  // Decimals are floored at 0 for the rare case where the uncertainty's
  // magnitude alone would call for negative decimal places (rounding to
  // tens/hundreds) — toFixed can't express that, so the value and
  // uncertainty are shown as whole numbers instead.
  //
  // `maxDecimals` defaults to MAX_DECIMALS but can be raised by callers whose
  // quantity legitimately needs more (e.g. a period fit with many transits
  // can have a sub-1e-6-day uncertainty; capping at 6 decimals would round
  // that to "0.000000" and falsely claim an exact period).
  //
  // Falls back to auto(value) when no finite, non-zero uncertainty is given.
  function pair(value, uncertainty, sf, maxDecimals) {
    sf = sf || 1;
    maxDecimals = maxDecimals === undefined ? MAX_DECIMALS : maxDecimals;
    var u = Number(uncertainty);
    if (!isFinite(u) || u === 0) {
      var fallback = auto(value);
      return { value: fallback, uncertainty: "", decimals: null, text: fallback };
    }
    var uRounded = Number(Math.abs(u).toPrecision(sf));
    var decimals = sf - 1 - orderOfMagnitude(uRounded);
    decimals = Math.min(Math.max(decimals, 0), maxDecimals);
    var uncStr = uRounded.toFixed(decimals);
    var valueMissing = value === null || value === undefined || value === "";
    var v = Number(value);
    var valueStr = !valueMissing && isFinite(v) ? v.toFixed(decimals) : "";
    var text = valueStr === "" ? uncStr : valueStr + " ± " + uncStr;
    return { value: valueStr, uncertainty: uncStr, decimals: decimals, text: text };
  }

  return {
    MAX_DECIMALS: MAX_DECIMALS,
    sigFigs: sigFigs,
    fixed: fixed,
    fixedOrBlank: fixedOrBlank,
    auto: auto,
    pair: pair,
  };
})();
