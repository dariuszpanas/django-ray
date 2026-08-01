import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const themeScript = fs.readFileSync(
  fileURLToPath(
    new URL(
      "../../src/django_ray/static/django_ray/admin/sensitive_task_data_theme.js",
      import.meta.url,
    ),
  ),
  "utf8",
);

function appliedTheme(values = {}, { throwOnRead = false } = {}) {
  const reads = [];
  let writes = 0;
  const document = { documentElement: { dataset: {} } };
  const localStorage = {
    getItem(key) {
      reads.push(key);
      if (throwOnRead) throw new Error("storage unavailable");
      return Object.hasOwn(values, key) ? values[key] : null;
    },
    setItem() {
      writes += 1;
    },
  };

  vm.runInNewContext(themeScript, { document, localStorage });
  return { reads, theme: document.documentElement.dataset.theme, writes };
}

test("uses Django's valid plain theme before an Unfold preference", () => {
  assert.deepEqual(
    appliedTheme({ theme: "dark", adminTheme: '"light"' }),
    { reads: ["theme"], theme: "dark", writes: 0 },
  );
});

test("uses Unfold's JSON-encoded preference when Django has none", () => {
  assert.deepEqual(appliedTheme({ adminTheme: '"dark"' }), {
    reads: ["theme", "adminTheme"],
    theme: "dark",
    writes: 0,
  });
});

test("preserves explicit auto and light modes", () => {
  assert.equal(appliedTheme({ theme: "auto" }).theme, "auto");
  assert.equal(appliedTheme({ adminTheme: '"light"' }).theme, "light");
});

test("falls back to auto for invalid, malformed, or inaccessible storage", () => {
  for (const values of [
    { theme: "sepia", adminTheme: '"sepia"' },
    { adminTheme: "not-json" },
    { adminTheme: "true" },
  ]) {
    assert.equal(appliedTheme(values).theme, "auto");
  }
  assert.deepEqual(appliedTheme({}, { throwOnRead: true }), {
    reads: ["theme", "adminTheme"],
    theme: "auto",
    writes: 0,
  });
});
