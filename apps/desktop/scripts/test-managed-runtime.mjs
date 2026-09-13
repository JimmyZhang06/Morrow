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
  backendCommand: process.argv.includes("--mock-ai") ? [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/compatible_backend_fixture.py")]
    : process.argv.includes("--source") ? [resolve("../../.venv/Scripts/python.exe"), resolve("../../scripts/desktop_backend.py")]
      : [join(resources, "vistora-backend", "vistora-backend.exe")] };
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
  if (process.argv.includes("--source")) {
    assert.equal(capabilities.data.features.local_search, true);
    const status = await runtime.request({ path: "/v1/local-search/status" });
    assert.equal(status.data.enabled, false);
    const permission = await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: true, expected_policy_epoch: status.data.policy_epoch } });
    assert.equal(permission.status, 200, JSON.stringify(permission));
    const rebuilt = await runtime.request({ path: "/v1/local-search/rebuild", method: "POST" });
    assert.equal(rebuilt.status, 200, JSON.stringify(rebuilt));
    assert.equal(rebuilt.data.rebuilt, 1);
    const find = () => runtime.request({ path: "/v1/local-search/query", method: "POST", body: { query: "离线" } });
    assert.equal((await find()).data.items[0].entry_id, id);
    const unrelated = await runtime.request({ path: "/v1/entries", method: "POST", headers: { "Idempotency-Key": "search-unrelated" },
      body: { client_id: "search-unrelated", content: "周末去公园散步", memory_policy: "default" } });
    assert.equal(unrelated.status, 201, JSON.stringify(unrelated));
    assert.equal((await find()).data.items[0].entry_id, id);
    const revised = await runtime.request({ path: `/v1/entries/${unrelated.data.id}`, method: "PATCH",
      headers: { "If-Match": '"1"', "Idempotency-Key": "search-revision" }, body: { content: "在家听音乐" } });
    assert.equal(revised.status, 200, JSON.stringify(revised));
    const old = await runtime.request({ path: "/v1/local-search/query", method: "POST", body: { query: "公园" } });
    assert.equal(old.data.items.length, 0);
    await runtime.request({ path: `/v1/entries/${unrelated.data.id}`, method: "DELETE",
      headers: { "If-Match": '"2"', "Idempotency-Key": "search-delete" } });
    const currentStatus = await runtime.request({ path: "/v1/local-search/status" });
    const revoked = await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: false, expected_policy_epoch: currentStatus.data.policy_epoch } });
    assert.equal(revoked.status, 200, JSON.stringify(revoked));
    assert.equal((await find()).data.items.length, 0);
    const regrant = await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: true, expected_policy_epoch: revoked.data.policy_epoch } });
    assert.equal(regrant.status, 200, JSON.stringify(regrant));
    const reindexed = await runtime.request({ path: "/v1/local-search/rebuild", method: "POST" });
    assert.equal(reindexed.status, 200, JSON.stringify(reindexed));
    assert.equal((await find()).data.items[0].entry_id, id);
    const disabled = await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: false, expected_policy_epoch: regrant.data.policy_epoch } });
    assert.equal(disabled.status, 200, JSON.stringify(disabled));
    const enableAgain = await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: true, expected_policy_epoch: disabled.data.policy_epoch } });
    assert.equal(enableAgain.ok, true);
    const job = await runtime.request({ path: "/v1/local-search/index-job", method: "POST" });
    assert.equal(job.status, 202);
    let completed = false;
    for (let attempt = 0; attempt < 60; attempt++) {
      const progress = await runtime.request({ path: "/v1/local-search/index-job" });
      assert.equal(progress.ok, true);
      if (progress.data?.state === "completed") {
        assert.ok(progress.data.indexed >= 1); completed = true; break;
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    assert.equal(completed, true, "Durable index worker must finish without UI polling driving it");
    const lastStatus = await runtime.request({ path: "/v1/local-search/status" });
    await runtime.request({ path: "/v1/local-search/permission", method: "POST",
      body: { enabled: false, expected_policy_epoch: lastStatus.data.policy_epoch } });
    console.log("Local search: permissions, passages, incremental revisions, revoke and durable worker passed");
  }
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
    if (process.argv.includes("--mock-ai")) {
      const material = await runtime.request({ path: "/v1/entries", method: "POST",
        headers: { "Idempotency-Key": "chat-material" },
        body: { client_id: "chat-material", content: "散步回来后，我完成了项目原型。", memory_policy: "default" } });
      assert.equal(material.status, 201);
      const chat = await runtime.request({ path: "/v1/conversations", method: "POST",
        body: { entry_ids: [material.data.id], allow_history: true } });
      assert.equal(chat.status, 201, JSON.stringify(chat));
      const chatPath = `/v1/conversations/${chat.data.id}`;
      const waitTurn = async (expected, path = chatPath) => {
        for (let i = 0; i < 100; i++) {
          const value = await runtime.request({ path });
          assert.equal(value.ok, true);
          const last = value.data.turns.at(-1);
          if (last?.state === expected) return value.data;
          if (!["queued", "running"].includes(last?.state)) {
            assert.equal(last.state, expected, JSON.stringify(value.data));
            return value.data;
          }
          await new Promise(resolve => setTimeout(resolve, 300));
        }
        assert.fail("Conversation worker did not finish");
      };
      const requestId = crypto.randomUUID();
      const send = { path: `${chatPath}/turns`, method: "POST", body: { request_id: requestId, question: "结合记录，我可以关注什么？" } };
      const queued = await runtime.request(send);
      assert.equal(queued.status, 202, JSON.stringify(queued));
      assert.equal((await runtime.request(send)).data.id, queued.data.id);
      const answered = await waitTurn("completed");
      assert.equal(answered.turns.length, 1);
      assert.equal(answered.turns[0].reply.citations[0].quote, "散步回来后，我完成了项目原型。");
      assert.equal((await runtime.request({ path: "/v1/entries" })).data.items.some(item => item.content === send.body.question), false);
      await runtime.stop(); runtime = new ManagedRuntime(options); await runtime.initialize();
      assert.equal((await runtime.request({ path: chatPath })).data.turns[0].reply.answer, answered.turns[0].reply.answer);
      assert.equal((await runtime.request({ path: `${chatPath}/turns`, method: "POST",
        body: { request_id: crypto.randomUUID(), question: "继续，我们还能确认什么？" } })).status, 202);
      await waitTurn("completed");
      await runtime.request({ path: `${chatPath}/turns`, method: "POST",
        body: { request_id: crypto.randomUUID(), question: "无效引用测试" } });
      const failed = await waitTurn("failed");
      assert.equal(failed.turns.at(-1).reply, null);
      await runtime.request({ path: `${chatPath}/turns`, method: "POST",
        body: { request_id: crypto.randomUUID(), question: "等待取消测试" } });
      await waitTurn("running");
      assert.equal((await runtime.request({ path: `${chatPath}/cancel`, method: "POST" })).status, 204);
      await new Promise(resolve => setTimeout(resolve, 2500));
      const canceled = await waitTurn("canceled");
      assert.equal(canceled.turns.at(-1).reply, null);

      assert.equal((await runtime.request({ path: `/v1/entries/${material.data.id}`, method: "DELETE",
        headers: { "If-Match": '"1"', "Idempotency-Key": "chat-source-delete" } })).ok, true);
      const hidden = await runtime.request({ path: chatPath });
      assert.equal(hidden.data.blocked, true);
      assert.equal(hidden.data.turns.every(turn => turn.reply === null), true);
      assert.equal((await runtime.request({ path: chatPath, method: "DELETE" })).status, 204);
      assert.equal((await runtime.request({ path: "/v1/conversations" })).data.items.length, 0);
      const memoryChat = await runtime.request({ path: "/v1/conversations", method: "POST",
        body: { entry_ids: [note.data.id], allow_history: true, include_reviewed_memories: true } });
      assert.equal(memoryChat.status, 201);
      const memoryPath = `/v1/conversations/${memoryChat.data.id}`;
      await runtime.request({ path: `${memoryPath}/turns`, method: "POST",
        body: { request_id: crypto.randomUUID(), question: "核对已确认认识" } });
      const firstReviewed = await waitTurn("completed", memoryPath);
      assert.equal(firstReviewed.turns[0].reply.reviewed_memories[0].review, "confirm");
      const latestMemory = await runtime.request({ path: `/v1/memories/${job.data.memory_id}` });
      const corrected = await runtime.request({ path: `/v1/memories/${job.data.memory_id}/verdicts`, method: "POST",
        headers: { "If-Match": latestMemory.headers.etag }, body: { verdict: "correct",
          replacement: { statement: "我只在陌生场合需要提前写要点。", mode: "interpretation_error" } } });
      assert.ok(corrected.ok, JSON.stringify(corrected));
      const afterCorrection = await runtime.request({ path: memoryPath });
      assert.equal(afterCorrection.data.turns[0].state, "outdated");
      assert.equal(afterCorrection.data.turns[0].reply, null);
      await runtime.request({ path: `${memoryPath}/turns`, method: "POST",
        body: { request_id: crypto.randomUUID(), question: "核对纠正后的认识" } });
      const correctedAnswer = await waitTurn("completed", memoryPath);
      assert.equal(correctedAnswer.turns.at(-1).reply.reviewed_memories[0].statement, "我只在陌生场合需要提前写要点。");
      await runtime.request({ path: memoryPath, method: "DELETE" });
      console.log("Reviewed chat context: confirmed interpretation, correction source, stale reply hiding and refreshed model history passed");
      console.log("Governed chat: scoped sources, exact citations, idempotency, restart/history, invalid quote, in-flight cancellation and deletion passed");
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
