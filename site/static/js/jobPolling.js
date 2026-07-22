window.MuscatJobPolling = (function () {
  function createTimeoutPoller(pollFn) {
    var timer = null;

    return {
      stop: function () {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
      },
      schedule: function (delayMs) {
        if (timer !== null) return;
        timer = setTimeout(function () {
          timer = null;
          pollFn();
        }, delayMs);
      },
      isScheduled: function () {
        return timer !== null;
      },
    };
  }

  return {
    createTimeoutPoller: createTimeoutPoller,
  };
})();
