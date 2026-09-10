import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { profiles, publicProfile, configureProfiles } = require("../electron/model-profiles.cjs");
const original = { ai: { enabled: false, model: "step-3.7-flash", key: "legacy-secret" } };
assert.equal(profiles(original)[0].key, "legacy-secret");
const input = { enabled: true, consent: true, profileId: "new", protocol: "compatible", name: "First",
  baseUrl: "https://one.example/v1", model: "vendor/model:latest", key: "first-secret" };
const first = configureProfiles(original, input);
const second = configureProfiles(first, { ...input, name: "Second", baseUrl: "https://two.example/v1", key: "second-secret" });
const back = configureProfiles(second, { ...input, profileId: first.ai.profileId, key: "" });
assert.equal(back.ai.key, "first-secret");
assert.notEqual(first.ai.activationId, back.ai.activationId);
assert.equal(back.aiProfiles.length, 3);
assert.equal(JSON.stringify(back.aiProfiles.map(publicProfile)).includes("secret"), false);
assert.throws(() => configureProfiles(back, { ...input, profileId: first.ai.profileId, baseUrl: "https://new.example/v1", key: "" }), /重新输入密钥/);
assert.throws(() => configureProfiles(back, { ...input, consent: false }));
for (const baseUrl of ["http://one.example", "https://secret@one.example", "https://one.example?key=s", "https://one.example/chat/completions"]) {
  assert.throws(() => configureProfiles(back, { ...input, baseUrl }));
}
const disabled = configureProfiles(back, { enabled: false });
assert.equal(disabled.ai.key, "");
assert.equal(disabled.aiProfiles.find(p => p.id === second.ai.profileId).key, "second-secret");
assert.equal(original.ai.key, "legacy-secret");
console.log("Model profile migration, switching, key isolation and URL checks passed.");

assert.deepEqual(profiles({ ai: { enabled: false, key: "", model: "step-3.7-flash" } }), []);
const removedInactive = configureProfiles(back, { enabled: false, deleteProfileId: second.ai.profileId });
assert.equal(removedInactive.ai.enabled, true);
assert.equal(removedInactive.ai.key, "first-secret");
assert.equal(removedInactive.aiProfiles.some(p => p.id === second.ai.profileId), false);
const removedActive = configureProfiles(back, { enabled: false, deleteProfileId: first.ai.profileId });
assert.equal(removedActive.ai.enabled, false);
assert.equal(removedActive.ai.key, "");
assert.equal(removedActive.aiProfiles.some(p => p.id === first.ai.profileId), false);
assert.throws(() => configureProfiles(back, { enabled: false, deleteProfileId: "missing" }));
console.log("Empty first launch and active/inactive configuration deletion passed.");
