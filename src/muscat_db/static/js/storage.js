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
