window.MuscatStorage = (function () {
  function get(key, fallbackValue) {
    try {
      var value = localStorage.getItem(key);
      return value === null ? fallbackValue : value;
    } catch (error) {
      return fallbackValue;
    }
  }

  function getJSON(key, fallbackValue) {
    var value = get(key, null);
    if (value === null) return fallbackValue;
    try {
      return JSON.parse(value);
    } catch (error) {
      return fallbackValue;
    }
  }

  function set(key, value) {
    try {
      localStorage.setItem(key, value);
      return true;
    } catch (error) {
      return false;
    }
  }

  function setJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (error) {
      return false;
    }
  }

  function remove(key) {
    try {
      localStorage.removeItem(key);
      return true;
    } catch (error) {
      return false;
    }
  }

  return {
    get: get,
    getJSON: getJSON,
    set: set,
    setJSON: setJSON,
    remove: remove,
  };
})();

window.MuscatOptions = (function (storage) {
  function save(key, collectOptions) {
    return storage.setJSON(key, collectOptions());
  }

  function restore(key) {
    return storage.getJSON(key, null);
  }

  function bindPanel(key, panel) {
    if (!panel) return;
    var savedOpen = storage.getJSON(key, null);
    if (savedOpen === true) panel.open = true;
    else if (savedOpen === false) panel.open = false;
    panel.addEventListener("toggle", function () {
      storage.setJSON(key, panel.open);
    });
  }

  return {
    save: save,
    restore: restore,
    bindPanel: bindPanel,
  };
})(window.MuscatStorage);

window.MuscatRouteState = (function (storage) {
  var TARGET_KEY = "target:lastName";
  var PHOTOMETRY_KEY = "photometry:lastContext";
  var TRANSIT_FIT_KEY = "transitFit:lastContext";
  var EPHEMERIS_TARGET_KEY = "muscat-ephem-selected-target";

  function cleanText(value) {
    return value == null ? "" : String(value).trim();
  }

  function contextUrl(path, context) {
    if (!context || !context.inst) return path;
    var params = [];
    ["inst", "date", "target", "run"].forEach(function (key) {
      var value = cleanText(context[key]);
      if (value) params.push(key + "=" + encodeURIComponent(value));
    });
    return params.length ? path + "?" + params.join("&") : path;
  }

  function rememberTarget(targetName) {
    targetName = cleanText(targetName);
    if (!targetName) return false;
    return storage.set(TARGET_KEY, targetName);
  }

  function targetUrl() {
    var targetName = cleanText(storage.get(TARGET_KEY, ""));
    return targetName ? "/target?name=" + encodeURIComponent(targetName) : "/target";
  }

  function rememberPhotometry(context) {
    return storage.setJSON(PHOTOMETRY_KEY, context || {});
  }

  function photometryUrl() {
    return contextUrl("/photometry", storage.getJSON(PHOTOMETRY_KEY, null));
  }

  function rememberTransitFit(context) {
    return storage.setJSON(TRANSIT_FIT_KEY, context || {});
  }

  function transitFitUrl() {
    return contextUrl("/transit-fit", storage.getJSON(TRANSIT_FIT_KEY, null));
  }

  function ephemerisUrl() {
    var targets = storage.getJSON(EPHEMERIS_TARGET_KEY, null);
    if (!Array.isArray(targets)) {
      var single = cleanText(storage.get(EPHEMERIS_TARGET_KEY, ""));
      targets = single ? [single] : [];
    }
    targets = targets.map(cleanText).filter(Boolean).sort();
    if (!targets.length) return "/ephemeris";
    return "/ephemeris?targets=" + targets.map(encodeURIComponent).join("+");
  }

  function applyNavbar() {
    var links = [
      ["target-nav-link", targetUrl()],
      ["photometry-nav-link", photometryUrl()],
      ["transit-fit-nav-link", transitFitUrl()],
      ["ephemeris-nav-link", ephemerisUrl()],
    ];
    links.forEach(function (entry) {
      var el = document.getElementById(entry[0]);
      if (el) el.href = entry[1];
    });
  }

  return {
    applyNavbar: applyNavbar,
    rememberTarget: rememberTarget,
    rememberPhotometry: rememberPhotometry,
    rememberTransitFit: rememberTransitFit,
    targetUrl: targetUrl,
    photometryUrl: photometryUrl,
    transitFitUrl: transitFitUrl,
    ephemerisUrl: ephemerisUrl,
  };
})(window.MuscatStorage);
