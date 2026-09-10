import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { resolve, join } from "node:path";
import { tmpdir } from "node:os";
import crypto from "node:crypto";
const require = createRequire(import.meta.url);
const { ManagedRuntime, run } = require("../electron/managed-runtime.cjs");
const { sealBackup, openBackup } = require("../electron/private-backup.cjs");
const password = "synthetic-canary-password";
const key = crypto.randomBytes(32).toString("hex");
const sample = { version: 1, database: Buffer.from("PGDMPsynthetic").toString("base64"),
  identity: { principalId: crypto.randomUUID(), vaultId: crypto.randomUUID(), contentKey: key, sourceHmac: key, modelHmac: key },
  clientState: { "vistora.recordDraft": JSON.stringify("未发送的草稿") } };
const sealed = sealBackup(sample, password);
assert.equal(openBackup(sealed, password).clientState["vistora.recordDraft"], JSON.stringify("未发送的草稿"));
assert.throws(() => openBackup(sealed, "incorrect-password"));
const altered = JSON.parse(sealed.toString()); altered.data = `${altered.data[0] === "A" ? "B" : "A"}${altered.data.slice(1)}`;
assert.throws(() => openBackup(Buffer.from(JSON.stringify(altered)), password));
assert.throws(() => sealBackup(sample, "short"));
console.log("Encrypted backup contracts passed");
if (!process.argv.includes("--integration")) process.exit(0);

const resources = resolve("../../.data/release-runtime");
const directory = await mkdtemp(join(tmpdir(), "vistora-managed-test-"));
// Test encryption keeps even optional real-provider credentials off disk in plaintext.
// Real Electron separately tests Windows safeStorage (DPAPI).
const testKey = crypto.randomBytes(32);
const safeStorage = { isEncryptionAvailable: () => true,
  encryptString: (value) => {
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv("aes-256-gcm", testKey, iv);
    const ciphertext = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
    return Buffer.concat([iv, cipher.getAuthTag(), ciphertext]);
  },
  decryptString: (value) => {
    const decipher = crypto.createDecipheriv("aes-256-gcm", testKey, value.subarray(0, 12));
    decipher.setAuthTag(value.subarray(12, 28));
    return Buffer.concat([decipher.update(value.subarray(28)), decipher.final()]).toString("utf8");
  } };
const options = { directory, resources, safeStorage,
  backendCommand: process.argv.includes("--mock-ai") ? [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/compatible_backend_fixture.py")] : [join(resources, "vistora-backend", "vistora-backend.exe")] };
let runtime = new ManagedRuntime(options);
try {
  await runtime.initialize();
  if (process.argv.includes("--postgres")) {
    const c = runtime.config;
    await run(resolve("../../.venv/Scripts/python.exe"), ["-m", "pytest",
      "tests/platform/test_alembic_staging_integration.py", "tests/platform/test_postgres_security_integration.py"], {
      cwd: resolve("../.."), env: { ...process.env,
        TEST_POSTGRES_DSN: `postgresql+asyncpg://vistora_owner:${c.adminPassword}@127.0.0.1:${c.databasePort}/postgres` },
    });
    console.log("Real PostgreSQL migration rehearsal and non-owner RLS isolation passed");
  }
  assert.equal(runtime.status().enabled, false);
  const capabilities = await runtime.request({ path: "/health/capabilities" });
  assert.equal(capabilities.data.features.entries, true);
  assert.equal(capabilities.data.features.candidate_insights, false);
  const unauthenticated = await fetch(`http://127.0.0.1:${runtime.port}/v1/entries`, { headers: { "X-Vault-ID": runtime.config.vaultId } });
  assert.equal(unauthenticated.status, 401);
  const create = await runtime.request({ path: "/v1/entries", method: "POST", headers: { "Idempotency-Key": "canary-first" },
    body: { client_id: "canary-first", content: "试用版离线记录", memory_policy: "default" } });
  assert.equal(create.status, 201, JSON.stringify(create));
  const id = create.data.id;
  const legacyFile = join(directory, "synthetic-legacy.json");
  await writeFile(legacyFile, JSON.stringify({ schemaVersion: 1, entries: [{ id: "legacy-test", content: "旧版修订", captured_at: "2026-09-01T00:00:00Z",
    revisions: [{ content: "旧版原话" }, { content: "旧版修订" }] }] }));
  await runtime.migrateLegacy(legacyFile);
  await runtime.migrateLegacy(legacyFile);
  assert.equal((await runtime.request({ path: "/v1/entries" })).data.items.length, 2);
  if (process.argv.includes("--ai") || process.argv.includes("--mock-ai")) {
    if (process.argv.includes("--mock-ai")) {
      await runtime.configureAi({ enabled: true, consent: true, profileId: "new", name: "Synthetic HTTP fixture", protocol: "compatible",
        baseUrl: "https://model-fixture.example/v1", key: "synthetic-compatible-test", model: "vendor/model:latest" });
    } else {
      assert.ok(process.env.VISTORA_CANARY_API_KEY, "An explicitly configured canary key is required");
      await runtime.configureAi({ enabled: true, consent: true, key: process.env.VISTORA_CANARY_API_KEY, model: "step-3.7-flash" });
    }
    const note = await runtime.request({ path: "/v1/entries", method: "POST", headers: { "Idempotency-Key": "ai-note" }, body: {
      client_id: "ai-note", memory_policy: "default", content: "最近三次开会，我都先把想说的话写下来，再选择一个合适的时机发言。今天我也这样做了，表达比临时组织语言更清楚。我想继续观察提前写下要点是否对我有帮助。有时和熟悉的同事聊天，不准备也能说得很清楚。" } });
    assert.equal(note.status, 201);
    const command = { path: `/v1/entries/${note.data.id}/candidate-insights`, method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID(), "If-Match": '"1"' }, body: {} };
    let job = await runtime.request(command);
    assert.ok(job.ok, `Candidate HTTP ${job.status}`);
    for (let attempt = 0; attempt < 90 && !["succeeded", "failed", "denied", "unknown"].includes(job.data.status); attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      job = await runtime.request({ path: `/v1/candidate-insight-jobs/${job.data.job_id}` });
    }
    assert.equal(job.data.status, "succeeded", `AI candidate ended as ${job.data.status}; reason=${runtime.lastProviderFailure || "not_available"}`);
    const replay = await runtime.request(command);
    assert.equal(replay.data.memory_id, job.data.memory_id);
    const memory = await runtime.request({ path: `/v1/memories/${job.data.memory_id}` });
    assert.ok(memory.ok);
    const evidence = await runtime.request({ path: `/v1/memories/${job.data.memory_id}/evidence/${memory.data.evidence[0].id}/excerpt` });
    assert.ok(evidence.ok);
    const confirmed = await runtime.request({ path: `/v1/memories/${job.data.memory_id}/verdicts`, method: "POST",
      headers: { "If-Match": memory.headers.etag }, body: { verdict: "confirm" } });
    assert.ok(confirmed.ok);
    const action = await runtime.request({ path: `/v1/memories/${job.data.memory_id}/actions`, method: "POST", timeoutMs: 60000,
      headers: { "Idempotency-Key": crypto.randomUUID() }, body: {} });
    assert.equal(action.status, 201, `AI action HTTP ${action.status}`);
    let etag = action.headers.etag;
    for (const verdict of ["accept", "complete", "revoke"]) {
      const transition = await runtime.request({ path: `/v1/actions/${action.data.action_id}/verdicts`, method: "POST",
        headers: { "If-Match": etag, "Idempotency-Key": crypto.randomUUID() }, body: { verdict } });
      assert.ok(transition.ok);
      etag = transition.headers.etag;
    }
    await runtime.configureAi({ enabled: false });
    assert.equal(runtime.status().hasKey, false);
    console.log(`${process.argv.includes("--mock-ai") ? "Synthetic compatible HTTP transport" : "Real provider"}: candidate, idempotent replay, evidence, confirmation, AI action, complete/revoke, disable passed`);
  }
  const backupFile = join(directory, "canary.vistora");
  await runtime.backup(backupFile, password, sample.clientState);
  assert.equal((await readFile(backupFile, "utf8")).includes("试用版离线记录"), false);
  await runtime.request({ path: `/v1/entries/${id}`, method: "DELETE", headers: { "If-Match": '"1"', "Idempotency-Key": "canary-delete" } });
  assert.equal((await runtime.request({ path: `/v1/entries/${id}` })).status, 404);
  await assert.rejects(runtime.restore(backupFile, "incorrect-password"));
  assert.equal((await runtime.request({ path: `/v1/entries/${id}` })).status, 404);
  const restoredState = await runtime.restore(backupFile, password);
  assert.deepEqual(restoredState, sample.clientState);
  assert.equal((await runtime.request({ path: `/v1/entries/${id}` })).data.content, "试用版离线记录");
  await runtime.stop();
  runtime = new ManagedRuntime(options);
  await runtime.initialize();
  assert.equal((await runtime.request({ path: `/v1/entries/${id}` })).data.content, "试用版离线记录");
  console.log("Packaged backend + PostgreSQL: initialization, auth, CRUD, legacy migration, encrypted backup, restore, restart passed");
} finally {
  await runtime.stop().catch(() => {});
  // Keep the isolated synthetic directory for inspection; never delete user data.
  console.log(`Synthetic test directory: ${directory}`);
}
