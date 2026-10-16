/*
 * FinGuard browser API adapter.
 *
 * This file owns transport concerns only. It never invents task, portfolio,
 * order, or approval state; all such values come from ApplicationService via
 * the HTTP API. It deliberately has no configuration or credential storage.
 */
(function (root) {
  "use strict";

  class ApiError extends Error {
    constructor(message, options) {
      super(message);
      this.name = "FinGuardApiError";
      this.code = options && options.code ? options.code : "HTTP_ERROR";
      this.status = options && options.status != null ? options.status : null;
      this.details = options && options.details != null ? options.details : null;
      this.cause = options && options.cause ? options.cause : null;
    }

    get isVersionConflict() {
      return this.code === "VERSION_CONFLICT" || this.status === 409;
    }

    get isUnavailable() {
      return this.code === "SOURCE_UNAVAILABLE" || this.code === "SERVICE_UNAVAILABLE" || this.status === 404 || this.status === 501 || this.status === 503;
    }
  }

  function errorFromResponse(body, status) {
    const envelope = body && typeof body === "object" ? body.error : null;
    const detail = body && typeof body === "object" ? body.detail : null;
    const code = envelope && envelope.code ? envelope.code : (body && body.code ? body.code : (status === 409 ? "VERSION_CONFLICT" : status === 503 ? "SERVICE_UNAVAILABLE" : "HTTP_ERROR"));
    const message = envelope && envelope.message ? envelope.message : (typeof detail === "string" ? detail : `HTTP request failed (${status})`);
    return new ApiError(message, { code, status, details: body });
  }

  class FinGuardApi {
    constructor(options) {
      const config = options || {};
      this.baseUrl = String(config.baseUrl || "").replace(/\/$/, "");
      this.fetchImpl = config.fetchImpl || (typeof root.fetch === "function" ? root.fetch.bind(root) : null);
      if (typeof this.fetchImpl !== "function") {
        throw new ApiError("Fetch is unavailable in this browser", { code: "NETWORK_ERROR" });
      }
      this._decisionPromises = new Map();
      this._pollStop = null;
      this._pollGeneration = 0;
    }

    async _request(path, options) {
      const requestOptions = Object.assign({ headers: {} }, options || {});
      const headers = Object.assign({}, requestOptions.headers || {});
      if (requestOptions.body !== undefined && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
      }
      requestOptions.headers = headers;
      let response;
      try {
        response = await this.fetchImpl(`${this.baseUrl}${path}`, requestOptions);
      } catch (cause) {
        throw new ApiError("Network request failed. Check that the local API is running.", { code: "NETWORK_ERROR", cause });
      }

      let body = null;
      try {
        const raw = typeof response.text === "function" ? await response.text() : "";
        body = raw ? JSON.parse(raw) : null;
      } catch (cause) {
        if (!response.ok) {
          throw new ApiError(`HTTP request failed (${response.status})`, { code: "INVALID_RESPONSE", status: response.status, cause });
        }
        throw new ApiError("The API returned invalid JSON", { code: "INVALID_RESPONSE", status: response.status, cause });
      }
      if (!response.ok) {
        throw errorFromResponse(body, response.status);
      }
      // A frozen service may return a structured DecisionView error with a
      // successful HTTP status. Treat that envelope exactly like HTTP 409.
      if (body && body.error && (body.error.code === "VERSION_CONFLICT" || body.error.code === "SERVICE_UNAVAILABLE")) {
        throw errorFromResponse(body, body.error.code === "VERSION_CONFLICT" ? 409 : 503);
      }
      return body;
    }

    createTask(prompt, dataMode, allowPaperOrderProposals) {
      return this._request("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          prompt: String(prompt || ""),
          data_mode: dataMode || "demo",
          allow_paper_order_proposals: Boolean(allowPaperOrderProposals),
        }),
      });
    }

    getTask(runId, options) {
      return this._request(`/api/tasks/${encodeURIComponent(runId)}`, options);
    }

    getEvents(runId, afterSeq, options) {
      const seq = Number.isInteger(afterSeq) && afterSeq >= 0 ? afterSeq : 0;
      return this._request(`/api/tasks/${encodeURIComponent(runId)}/events?after_seq=${seq}`, options);
    }

    decideProposal(runId, proposalId, decision, expectedVersion) {
      const key = `${runId}:${proposalId}`;
      const existing = this._decisionPromises.get(key);
      if (existing) return existing;
      const promise = this._request(`/api/proposals/${encodeURIComponent(proposalId)}/decision`, {
        method: "POST",
        body: JSON.stringify({
          run_id: String(runId),
          decision: decision,
          expected_version: expectedVersion == null ? null : expectedVersion,
        }),
      }).finally(() => this._decisionPromises.delete(key));
      this._decisionPromises.set(key, promise);
      return promise;
    }

    resumeTask(runId) {
      return this._request(`/api/tasks/${encodeURIComponent(runId)}/resume`, { method: "POST" });
    }

    getPortfolio() {
      return this._request("/api/portfolio");
    }

    getPublicConfig() {
      return this._request("/api/config/public");
    }

    stopPolling() {
      this._pollGeneration += 1;
      if (this._pollStop) this._pollStop();
      this._pollStop = null;
    }

    startPolling(runId, callbacks, options) {
      const handlers = callbacks || {};
      const config = options || {};
      const intervalMs = Math.max(250, Number(config.intervalMs) || 1500);
      this.stopPolling();
      const generation = this._pollGeneration;
      let stopped = false;
      let timer = null;
      let afterSeq = Number.isInteger(config.afterSeq) && config.afterSeq >= 0 ? config.afterSeq : 0;
      const stop = () => {
        stopped = true;
        if (timer !== null) root.clearTimeout(timer);
        if (this._pollStop === stop) this._pollStop = null;
      };
      this._pollStop = stop;
      const active = () => !stopped && generation === this._pollGeneration;
      const tick = async () => {
        if (!active()) return;
        try {
          const task = await this.getTask(runId);
          if (!active()) return;
          const events = await this.getEvents(runId, afterSeq);
          if (!active()) return;
          for (const event of Array.isArray(events) ? events : []) {
            const seq = Number(event && event.seq);
            if (Number.isFinite(seq)) afterSeq = Math.max(afterSeq, seq);
          }
          if (typeof handlers.onUpdate === "function") handlers.onUpdate({ runId, task, events, afterSeq });
          if (["COMPLETED", "FAILED", "CANCELLED"].includes(task && task.status)) {
            stop();
            return;
          }
        } catch (error) {
          if (active() && typeof handlers.onError === "function") handlers.onError(error);
          stop();
          return;
        }
        if (active()) timer = root.setTimeout(tick, intervalMs);
      };
      tick();
      return stop;
    }
  }

  root.FinGuardApi = FinGuardApi;
  root.FinGuardApiError = ApiError;
})(typeof globalThis !== "undefined" ? globalThis : window);
