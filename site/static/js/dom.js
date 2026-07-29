/* Shared DOM helpers.
 *
 * These were duplicated across templates, and the copies had drifted. Most
 * importantly there were three different escapeHtml implementations feeding
 * user-facing tables, two of which mangled ordinary values:
 *
 *   input       base.html   jobs.html   lco_schedule.html
 *   0           ''          '0'         '0'
 *   false       ''          'false'     'false'
 *   null        ''          ''          'null'
 *   undefined   ''          ''          'undefined'
 *
 * All three escaped the same five characters, so this was never an XSS
 * difference -- but a zero silently vanished on one page and the literal text
 * "null" was rendered on another. The jobs.html semantics below are correct in
 * every case and are now the single definition.
 *
 * Loaded globally from base.html, so every page has window.MuscatDom.
 */
(function (global) {
  'use strict';

  function el(id) {
    return document.getElementById(id);
  }

  /* Escape for interpolation into HTML. null/undefined become '', every other
     value (including 0 and false) is stringified first so it survives. */
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* Two field readers, deliberately kept distinct: the LCO pages trim, while
     the photometry and transit-fit pages pass values through to pipeline
     arguments and have never trimmed. Unifying them would be a silent
     behaviour change on those pages, so each template picks the one it uses. */
  function val(id) {
    var e = el(id);
    return e ? e.value.trim() : '';
  }

  function valRaw(id) {
    var e = el(id);
    return e ? e.value : '';
  }

  function num(id) {
    var v = val(id);
    return v === '' ? null : Number(v);
  }

  function chk(id) {
    var e = el(id);
    return e ? !!e.checked : false;
  }

  function setVal(id, v) {
    var e = el(id);
    if (e && v !== undefined && v !== null) e.value = v;
  }

  function setChk(id, v) {
    var e = el(id);
    if (e) e.checked = !!v;
  }

  /* Status line used by the LCO pages: text plus a state class. */
  function msg(id, text, kind) {
    var e = el(id);
    if (!e) return;
    e.textContent = text || '';
    e.className = 'lco-msg' + (kind ? ' ' + kind : '');
  }

  global.MuscatDom = {
    el: el,
    escapeHtml: escapeHtml,
    val: val,
    valRaw: valRaw,
    num: num,
    chk: chk,
    setVal: setVal,
    setChk: setChk,
    msg: msg,
  };
})(window);
