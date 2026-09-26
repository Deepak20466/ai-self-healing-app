const test = require("node:test");
const assert = require("node:assert");
const { createApp } = require("../src/app");

function withServer(fn) {
  return new Promise((resolve, reject) => {
    const server = createApp().listen(0, async () => {
      try {
        await fn(`http://127.0.0.1:${server.address().port}`);
        resolve();
      } catch (e) {
        reject(e);
      } finally {
        server.close();
      }
    });
  });
}

test("known user gets an upper-cased display name", () =>
  withServer(async (base) => {
    const res = await fetch(`${base}/users/1/display-name`);
    assert.strictEqual(res.status, 200);
    assert.deepStrictEqual(await res.json(), { name: "ADA" });
  }));

test("unknown user does not crash the server", () =>
  withServer(async (base) => {
    const res = await fetch(`${base}/users/999/display-name`);
    assert.notStrictEqual(res.status, 500);
  }));
