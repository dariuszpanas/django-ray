import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPOSITORY_ROOT = fileURLToPath(new URL("..", import.meta.url));
const NPM_CLI = process.env.npm_execpath;

assert.ok(NPM_CLI, "run these fixtures through `npm test`");

const DESCRIPTIVE_BODY = [
  "Keep lease ownership attached to the active worker generation so stale",
  "workers cannot publish a late result after replacement. This preserves",
  "deterministic recovery across all supported execution backends.",
].join("\n");

function commitMessage(header, { body = DESCRIPTIVE_BODY, trailers } = {}) {
  const sections = [header, "", body];
  if (trailers !== null) {
    sections.push("", ...(trailers ?? ["Validation: focused commit policy tests passed."]));
  }
  return `${sections.join("\n")}\n`;
}

function runPolicy(script, message) {
  const result = spawnSync(
    process.execPath,
    [NPM_CLI, "run", "--silent", script],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: { ...process.env, FORCE_COLOR: "0", NO_COLOR: "1" },
      input: message,
    },
  );
  assert.ifError(result.error);
  return {
    output: `${result.stdout ?? ""}${result.stderr ?? ""}`,
    status: result.status,
  };
}

function assertAccepted(message, script = "commitlint") {
  const result = runPolicy(script, message);
  assert.equal(result.status, 0, result.output);
}

function assertRejected(message, rule, script = "commitlint") {
  const result = runPolicy(script, message);
  assert.notEqual(result.status, 0, "commitlint unexpectedly accepted the fixture");
  if (rule !== null) {
    assert.match(result.output, new RegExp(`\\[${rule}\\]`));
  }
}

function runMakeTitle(title) {
  const env = { ...process.env, FORCE_COLOR: "0", NO_COLOR: "1" };
  if (title === undefined) {
    delete env.PR_TITLE;
  } else {
    env.PR_TITLE = title;
  }
  const result = spawnSync("make", ["commit-title-check"], {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    env,
  });
  assert.ifError(result.error);
  return {
    output: `${result.stdout ?? ""}${result.stderr ?? ""}`,
    status: result.status,
  };
}

test("accepts a descriptive Conventional Commit", () => {
  assertAccepted(commitMessage("feat(worker): preserve bounded lease ownership"));
});

test("rejects a header-only commit", () => {
  assertRejected("fix(worker): preserve lease ownership\n", "body-empty");
});

test("rejects a body shorter than 100 characters", () => {
  assertRejected(
    commitMessage("fix(worker): preserve lease ownership", {
      body: "Keep active lease ownership stable during worker recovery.",
    }),
    "body-min-length",
  );
});

test("rejects a commit without a Validation trailer", () => {
  assertRejected(
    commitMessage("fix(worker): preserve lease ownership", { trailers: null }),
    "validation-trailer",
  );
});

test("rejects an empty Validation trailer", () => {
  assertRejected(
    commitMessage("fix(worker): preserve lease ownership", {
      trailers: ["Validation:"],
    }),
    "validation-trailer",
  );
});

test("rejects prose lines longer than 72 characters", () => {
  const longLine =
    "This deliberately overlong prose line exceeds the repository's narrow history limit.";
  assert.ok(longLine.length > 72);
  assertRejected(
    commitMessage("docs: clarify lease ownership", {
      body: `${DESCRIPTIVE_BODY}\n${longLine}`,
    }),
    "body-max-line-length",
  );
});

test("allows a long URL line in the commit body", () => {
  const longUrl =
    "https://example.com/this/is/a/deliberately/long/reference/path/that/exceeds/seventy-two/characters";
  assert.ok(longUrl.length > 72);
  assertAccepted(
    commitMessage("docs: link lease ownership details", {
      body: `${DESCRIPTIVE_BODY}\n${longUrl}`,
    }),
  );
});

test("accepts a breaking exclamation mark without a breaking footer", () => {
  assertAccepted(commitMessage("feat(worker)!: retire legacy lease ownership"));
});

for (const separator of ["BREAKING CHANGE", "BREAKING-CHANGE"]) {
  test(`accepts a ${separator} footer without an exclamation mark`, () => {
    assertAccepted(
      commitMessage("feat(worker): replace the legacy ownership protocol", {
        trailers: [
          `${separator}: workers must send the current ownership generation.`,
          "Validation: focused worker protocol tests passed.",
        ],
      }),
    );
  });
}

for (const header of [
  "fixup! feat(worker): preserve lease ownership",
  'Revert "feat(worker): preserve lease ownership"',
]) {
  test(`does not silently ignore ${header.split(" ", 1)[0]} commits`, () => {
    assertRejected(commitMessage(header), null);
  });
}

test("accepts a valid title without requiring a body", () => {
  assertAccepted("fix(worker): preserve lease ownership\n", "commitlint:title");
});

test("rejects a non-Conventional PR title", () => {
  assertRejected("Preserve lease ownership\n", "type-empty", "commitlint:title");
});

test("Make validates a PR title from the environment", () => {
  const result = runMakeTitle("fix(worker): preserve lease ownership");
  assert.equal(result.status, 0, result.output);
});

test("Make rejects a non-Conventional PR title from the environment", () => {
  const result = runMakeTitle("Preserve lease ownership");
  assert.notEqual(result.status, 0, "Make unexpectedly accepted the invalid title");
  assert.match(result.output, /\[type-empty\]/u);
});

test("Make rejects a missing PR title", () => {
  const result = runMakeTitle(undefined);
  assert.notEqual(result.status, 0, "Make unexpectedly accepted a missing title");
  assert.match(result.output, /PR_TITLE is required\./u);
});
