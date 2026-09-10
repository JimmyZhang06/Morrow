const crypto = require("node:crypto");

function validatePassword(password) {
  if (typeof password !== "string" || password.length < 12 || password.length > 1024) {
    throw new Error("备份密码需为 12 至 1024 个字符，请妥善保存，无法找回");
  }
}

function validatePayload(payload) {
  const identity = payload?.identity;
  if (payload?.version !== 1 || typeof payload.database !== "string" || !identity
    || !/^[0-9a-f-]{36}$/.test(identity.principalId) || !/^[0-9a-f-]{36}$/.test(identity.vaultId)
    || [identity.contentKey, identity.sourceHmac, identity.modelHmac].some((value) => !/^[a-f0-9]{64}$/.test(value))
    || !Buffer.from(payload.database, "base64").subarray(0, 5).equals(Buffer.from("PGDMP"))) {
    throw new Error("备份内容或版本无效");
  }
  // Only known identity fields may influence the restored runtime configuration.
  payload.identity = { principalId: identity.principalId, vaultId: identity.vaultId,
    contentKey: identity.contentKey, sourceHmac: identity.sourceHmac, modelHmac: identity.modelHmac,
    legacyImported: identity.legacyImported === true };
  const clientState = payload.clientState;
  if (clientState !== undefined && (typeof clientState !== "object" || clientState === null
    || Object.entries(clientState).some(([key, value]) =>
      !["vistora.localEntries", "vistora.recordDraft", "vistora.localActions.v2"].includes(key) || typeof value !== "string"))) {
    throw new Error("备份中的本机草稿格式无效");
  }
  return payload;
}

function sealBackup(payload, password) {
  validatePassword(password);
  validatePayload(payload);
  const salt = crypto.randomBytes(16);
  const iv = crypto.randomBytes(12);
  const key = crypto.scryptSync(password, salt, 32);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  cipher.setAAD(Buffer.from("VISTORA_BACKUP_V1"));
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(payload), "utf8"), cipher.final()]);
  return Buffer.from(JSON.stringify({ format: "VISTORA_BACKUP_V1", salt: salt.toString("base64"),
    iv: iv.toString("base64"), tag: cipher.getAuthTag().toString("base64"), data: ciphertext.toString("base64") }));
}

function openBackup(bytes, password) {
  validatePassword(password);
  try {
    const envelope = JSON.parse(bytes.toString("utf8"));
    if (envelope.format !== "VISTORA_BACKUP_V1") throw new Error("version");
    const salt = Buffer.from(envelope.salt, "base64");
    const iv = Buffer.from(envelope.iv, "base64");
    const tag = Buffer.from(envelope.tag, "base64");
    if (salt.length !== 16 || iv.length !== 12 || tag.length !== 16) throw new Error("shape");
    const decipher = crypto.createDecipheriv("aes-256-gcm", crypto.scryptSync(password, salt, 32), iv);
    decipher.setAAD(Buffer.from("VISTORA_BACKUP_V1"));
    decipher.setAuthTag(tag);
    const plain = Buffer.concat([decipher.update(Buffer.from(envelope.data, "base64")), decipher.final()]);
    return validatePayload(JSON.parse(plain.toString("utf8")));
  } catch { throw new Error("备份密码不正确、文件损坏或格式不受支持，当前数据未改变"); }
}

module.exports = { sealBackup, openBackup };
