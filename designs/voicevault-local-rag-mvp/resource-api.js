(function () {
  async function request(path, options) {
    let response;
    try {
      response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
    } catch (_error) {
      throw new Error("本地服务未连接");
    }
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new Error("本地服务返回了无效 JSON");
    }
    if (!response.ok || !payload.ok) {
      const error = payload.error || {};
      const message = error.message || `HTTP ${response.status}`;
      const failure = new Error(message);
      failure.code = error.code || "request_failed";
      throw failure;
    }
    return payload;
  }

  function get(path) {
    return request(path, { method: "GET", headers: {} });
  }

  function post(path, body) {
    return request(path, { method: "POST", body: JSON.stringify(body || {}) });
  }

  async function poll(path, terminal, onUpdate, delayMs = 500) {
    for (;;) {
      const payload = await get(path);
      onUpdate(payload);
      if (terminal(payload)) return payload;
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }

  window.VoiceVaultApi = { get, post, poll };
})();
