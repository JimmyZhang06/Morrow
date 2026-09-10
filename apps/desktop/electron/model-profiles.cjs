const crypto = require("node:crypto");
const STEP_BASE = "https://api.stepfun.com/step_plan/v1";

function profiles(config) {
  if (Array.isArray(config.aiProfiles)) return config.aiProfiles;
  if (!config.ai.enabled && !config.ai.key) return [];
  return [{ id: "legacy-stepfun", name: "Step Plan", protocol: "stepfun", baseUrl: STEP_BASE,
    jsonMode: true, model: config.ai.model, key: config.ai.key }];
}

function publicProfile(profile) {
  const { id, name, protocol, baseUrl, model, jsonMode } = profile;
  return { id, name, protocol, baseUrl, model, jsonMode, hasKey: Boolean(profile.key) };
}

function configureProfiles(config, input) {
  if (typeof input.enabled !== "boolean" || (input.enabled && input.consent !== true)) {
    throw new Error("请先确认所选模型服务的数据说明");
  }
  const saved = profiles(config);
  if (input.deleteProfileId !== undefined) {
    if (typeof input.deleteProfileId !== "string" || !saved.some(p => p.id === input.deleteProfileId)) throw new Error("模型配置不存在，请刷新设置");
    const active = input.deleteProfileId === (config.ai.profileId || "legacy-stepfun");
    return { ...config, aiProfiles: saved.filter(p => p.id !== input.deleteProfileId),
      ai: active ? { enabled: false, key: "", model: "", profileId: "" } : config.ai };
  }
  if (!input.enabled) {
    const activeId = config.ai.profileId || "legacy-stepfun";
    return { ...config, ai: { ...config.ai, enabled: false, key: "" },
      aiProfiles: saved.map(p => p.id === activeId ? { ...p, key: "" } : p) };
  }
  const previous = saved.find(p => p.id === (input.profileId || "legacy-stepfun"));
  if (input.profileId && input.profileId !== "new" && !previous) throw new Error("模型配置不存在，请刷新设置");
  const protocol = input.protocol || previous?.protocol || (input.profileId === "new" ? "compatible" : "stepfun");
  if (!["compatible", "stepfun"].includes(protocol)) throw new Error("不支持的 API 协议");
  const raw = String(input.baseUrl || previous?.baseUrl || STEP_BASE).trim();
  let url;
  try { url = new URL(raw); } catch { throw new Error("请填写有效的 HTTPS API 服务地址"); }
  if (raw.length > 2048 || /[\s\\]/.test(raw) || url.protocol !== "https:" || url.username || url.password || raw.includes("?") || raw.includes("#") || url.pathname.replace(/\/+$/, "").endsWith("/chat/completions")) {
    throw new Error("请填写 HTTPS API 根地址，不含密钥、查询参数或 /chat/completions");
  }
  const baseUrl = url.href.replace(/\/+$/, "");
  if (protocol === "stepfun" && ![STEP_BASE, "https://api.stepfun.ai/step_plan/v1"].includes(baseUrl)) throw new Error("Step Plan 请使用对应的专用服务地址，其他服务请选择通用兼容协议");
  const model = String(input.model || previous?.model || "").trim();
  const name = String(input.name || previous?.name || model).trim();
  const canReuse = previous?.baseUrl === baseUrl && previous?.protocol === protocol;
  const key = typeof input.key === "string" && input.key.trim() ? input.key.trim() : (canReuse ? previous.key : "");
  if (!key || key.length > 4096 || /\s/.test(key) || !/^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$/.test(model) || !name || name.length > 80) {
    throw new Error("请填写配置名称、有效的模型名称和 API Key；更换服务地址后需重新输入密钥");
  }
  if (!previous && saved.length >= 20) throw new Error("最多保存 20 个模型配置，请编辑已有配置");
  const profile = { id: previous?.id || crypto.randomUUID(), name, protocol, baseUrl, model, key, jsonMode: input.jsonMode === true };
  return { ...config, aiProfiles: [...saved.filter(p => p.id !== profile.id), profile],
    ai: { ...profile, enabled: true, profileId: profile.id, activationId: crypto.randomUUID() } };
}

module.exports = { profiles, publicProfile, configureProfiles };
