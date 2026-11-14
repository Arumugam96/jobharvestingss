import { request, isTransientError, ApiError } from "./api";

describe("request() header merge", () => {
  const realFetch = global.fetch;
  afterEach(() => { global.fetch = realFetch; });

  test("caller headers are merged over the defaults, not replacing them", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: 1 }) });
    const body = await request("/health", { method: "POST", headers: { "X-Extra": "1" }, body: "{}" });
    expect(body).toEqual({ ok: 1 });
    const [, init] = global.fetch.mock.calls[0];
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    expect(init.credentials).toBe("include");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.headers["X-Dev-Tenant"]).toBeTruthy();
    expect(init.headers["X-Extra"]).toBe("1");
  });

  test("a caller header can still override a default", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 200, json: async () => null });
    await request("/health", { headers: { "Content-Type": "text/plain" } });
    const [, init] = global.fetch.mock.calls[0];
    expect(init.headers["Content-Type"]).toBe("text/plain");
    expect(init.headers["X-Dev-Tenant"]).toBeTruthy();
  });
});

describe("isTransientError", () => {
  test.each([408, 429, 500, 502, 503, 504])("ApiError %i is transient", (status) => {
    expect(isTransientError(new ApiError("x", status, null))).toBe(true);
  });
  test.each([400, 401, 403, 404, 409, 422])("ApiError %i is not transient", (status) => {
    expect(isTransientError(new ApiError("x", status, null))).toBe(false);
  });
  test("a network failure (fetch rejected) is transient", () => {
    expect(isTransientError(new TypeError("Failed to fetch"))).toBe(true);
  });
});
