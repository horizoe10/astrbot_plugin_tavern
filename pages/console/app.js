import { bridge } from "./core/bridge.js";
import {
  $,
  $$,
  escapeHTML,
  formatBytes,
  formatDate,
  prettyJSON,
  snapshotKindLabel,
  statusBadge,
  statusLabel,
} from "./core/dom.js";
import { app, viewMeta } from "./core/state.js";

const DEFAULT_CHARACTER_CARD_TEMPLATE = {
  version: 4,
  auto_approve: false,
  edit_requires_review: true,
  fields: [
    ["name", "角色姓名", true, false, 12],
    ["code", "代号 / 昵称", true, false, 12],
    ["appearance", "外貌特征", true, false, 300],
    ["background", "角色背景", true, false, 800],
    ["personality", "性格与行事方式", true, false, 400],
    ["goal", "当前目标", true, false, 300],
    ["belief", "核心信念", true, false, 300],
    ["bond", "重要羁绊", true, false, 300],
    ["specialties", "专长标签", true, false, 240],
    ["flaws", "缺陷标签", true, false, 200],
    ["weakness", "弱点或限制", true, false, 300],
    ["knowledge_boundary", "知识边界", true, false, 400],
    ["secret", "私人秘密", false, true, 600],
    ["content_boundaries", "个人内容边界", false, true, 600],
  ].map(([key, label, required, privateField, max_chars]) => ({
    key,
    label,
    required,
    private: privateField,
    max_chars,
  })),
  stats: {
    mode: "manual",
    budget: 10,
    attributes: [
      ["body", "体魄"],
      ["agility", "敏捷"],
      ["will", "意志"],
      ["knowledge", "学识"],
    ].map(([key, label]) => ({
      key,
      label,
      minimum: 0,
      maximum: 5,
      default: 2,
    })),
    modifier_table: { 0: -3, 1: -2, 2: -1, 3: 0, 4: 1, 5: 2 },
  },
};

function validateCharacterCardTemplate(template) {
  if (!template || typeof template !== "object" || Array.isArray(template)) {
    throw new Error("玩家角色卡模板必须是 JSON 对象");
  }
  if (!Number.isInteger(Number(template.version)) || Number(template.version) < 1) {
    throw new Error("角色卡模板 version 必须是大于 0 的整数");
  }
  if (!Array.isArray(template.fields) || !template.fields.length) {
    throw new Error("角色卡模板必须包含 fields 数组");
  }
  const keys = template.fields.map((field) => String(field?.key || "").trim());
  if (keys.some((key) => !key)) throw new Error("角色卡字段 key 不能为空");
  if (new Set(keys).size !== keys.length) throw new Error("角色卡字段 key 不能重复");
  for (const required of ["name", "code"]) {
    if (!keys.includes(required)) throw new Error(`角色卡缺少必需字段 ${required}`);
  }
  const mode = String(template.stats?.mode || "manual").toLowerCase();
  if (!["none", "manual", "preset", "preset_stack"].includes(mode)) {
    throw new Error("数值模式必须是 none、manual、preset 或 preset_stack");
  }
  const attributes = Array.isArray(template.stats?.attributes)
    ? template.stats.attributes
    : [];
  if (mode !== "none" && !attributes.length) {
    throw new Error("启用数值系统时必须包含 stats.attributes");
  }
  if (mode === "none") return template;
  if (mode === "preset_stack") {
    const generation = template.stat_generation || template.stats?.stat_generation;
    if (!generation || generation.mode !== "preset_stack") {
      throw new Error("preset_stack 必须声明 character_card.stat_generation");
    }
    if (
      !generation.base_stats ||
      !Array.isArray(generation.bonus_sources) ||
      !generation.bonus_sources.length
    ) {
      throw new Error("preset_stack 必须声明 base_stats 与 bonus_sources");
    }
    if (generation.allow_manual_edit !== false) {
      throw new Error("preset_stack 必须设置 allow_manual_edit=false");
    }
    // Canonicalize the editor value without deleting the compatibility copy.
    template.stat_generation = generation;
  }
  const minimumBudget = attributes.reduce(
    (sum, item) => sum + Number(item.minimum || 0),
    0,
  );
  const maximumBudget = attributes.reduce(
    (sum, item) => sum + Number(item.maximum || 0),
    0,
  );
  const budget = Number(template.stats?.budget);
  if (!Number.isFinite(budget) || budget < minimumBudget || budget > maximumBudget) {
    throw new Error(`属性预算必须介于 ${minimumBudget} 与 ${maximumBudget} 之间`);
  }
  return template;
}

function parseJSONField(id, label) {
  const raw = $(id).value.trim();
  try {
    const parsed = JSON.parse(raw || "{}");
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("根节点不是对象");
    }
    return parsed;
  } catch (error) {
    throw new Error(`${label} 不是有效的 JSON 对象：${error.message}`);
  }
}

function turnPlayerStatus(player, turn) {
  const order = Array.isArray(turn?.order) ? turn.order : [];
  const position = order.findIndex((item) => item.user_id === player.user_id);
  if (position < 0) return player.enabled ? "未加入轮次" : "已停用";
  const current = turn.current_user_id === player.user_id ? " · 当前行动者" : "";
  return `第 ${position + 1} 位${current}`;
}

function turnPlayerControls(player, turn) {
  const order = Array.isArray(turn?.order) ? turn.order : [];
  const position = order.findIndex((item) => item.user_id === player.user_id);
  if (position < 0) {
    return player.enabled
      ? `<button class="action-button" data-session-detail-action="turn-add"
          data-user-id="${escapeHTML(player.user_id)}">加入顺序</button>`
      : "";
  }
  return `
    <button class="action-button" data-session-detail-action="turn-up"
      data-user-id="${escapeHTML(player.user_id)}" ${
        position === 0 ? "disabled" : ""
      }>上移</button>
    <button class="action-button" data-session-detail-action="turn-down"
      data-user-id="${escapeHTML(player.user_id)}" ${
        position === order.length - 1 ? "disabled" : ""
      }>下移</button>
    <button class="action-button is-danger" data-session-detail-action="turn-remove"
      data-user-id="${escapeHTML(player.user_id)}">移出</button>
  `;
}

const CHARACTER_CARD_STATUS_LABELS = {
  uncreated: "未建卡",
  draft: "建卡中",
  pending_review: "待审核",
  approved: "已通过",
  rejected: "未通过",
};

const PARTICIPANT_STATUS_LABELS = {
  reserved: "占位",
  active: "出场",
  standby: "候补",
  away: "暂离",
  retired: "已退场",
  archived: "已归档",
};

const CHARACTER_RUNTIME_LABELS = {
  inspiration: "当前灵感",
  inspiration_max: "灵感上限",
  statuses: "当前状态",
  equipment: "装备与携带物",
  known_clues: "已知线索",
  npc_relationships: "NPC 关系",
  temporary_traits: "临时特质",
  reputation: "声望",
  current_location: "角色位置",
};

function hasCharacterCardValue(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function characterCardValueHTML(value, emptyLabel = "未填写") {
  if (!hasCharacterCardValue(value)) {
    return `<span class="character-card-empty">${escapeHTML(emptyLabel)}</span>`;
  }
  if (typeof value === "boolean") {
    return `<span>${value ? "是" : "否"}</span>`;
  }
  if (typeof value === "object") {
    return `<pre>${escapeHTML(JSON.stringify(value, null, 2))}</pre>`;
  }
  return `<span>${escapeHTML(value)}</span>`;
}

function characterCardFieldClass(value) {
  const text =
    typeof value === "object" && value !== null
      ? JSON.stringify(value)
      : String(value ?? "");
  return text.length > 72 || text.includes("\n") ? " is-wide" : "";
}

function resolvedCharacterCardTemplate(detail) {
  const resolved = detail.instance_config?.character_card_template;
  if (
    resolved &&
    Array.isArray(resolved.fields) &&
    Array.isArray(resolved.stats?.attributes)
  ) {
    return resolved;
  }
  return DEFAULT_CHARACTER_CARD_TEMPLATE;
}

function characterCardProfile(item) {
  if (
    item.card_profile &&
    typeof item.card_profile === "object" &&
    Object.keys(item.card_profile).length
  ) {
    return item.card_profile;
  }
  if (item.draft_profile && typeof item.draft_profile === "object") {
    return item.draft_profile;
  }
  return {};
}

function renderCharacterCardFields(profile, template) {
  const definitions = Array.isArray(template.fields) ? template.fields : [];
  const visibleDefinitions = definitions.filter(
    (field) => !field.stat_key && !String(field.key || "").startsWith("stat_"),
  );
  const knownKeys = new Set(definitions.map((field) => String(field.key || "")));
  const extraFields = Object.entries(profile)
    .filter(
      ([key]) =>
        !knownKeys.has(key) &&
        !String(key).startsWith("stat_") &&
        !String(key).startsWith("_") &&
        !["resolved_stat_total", "profession_base_stats"].includes(key),
    )
    .map(([key, value]) => ({
      key,
      label: key,
      required: false,
      private: false,
      value,
    }));
  const fields = [
    ...visibleDefinitions.map((field) => ({
      ...field,
      value: profile[field.key],
    })),
    ...extraFields,
  ];
  if (!fields.length) {
    return '<div class="empty-inline">当前模板没有资料字段。</div>';
  }
  return fields
    .map(
      (field) => `
        <article class="character-card-field${characterCardFieldClass(field.value)}">
          <div class="character-card-field-label">
            <span>${escapeHTML(field.label || field.key)}</span>
            ${field.required ? '<em class="is-required">必填</em>' : ""}
            ${field.private ? '<em class="is-private">私密</em>' : ""}
          </div>
          <div class="character-card-field-value">
            ${characterCardValueHTML(field.value)}
          </div>
        </article>
      `,
    )
    .join("");
}

function renderCharacterCardStats(profile, stats, template) {
  const definition = template.stats || {};
  const attributes = Array.isArray(definition.attributes)
    ? definition.attributes
    : [];
  const raw = stats?.raw && typeof stats.raw === "object" ? stats.raw : {};
  const labels =
    stats?.labels && typeof stats.labels === "object" ? stats.labels : {};
  const modifiers =
    stats?.modifiers && typeof stats.modifiers === "object"
      ? stats.modifiers
      : {};
  const table =
    definition.modifier_table && typeof definition.modifier_table === "object"
      ? definition.modifier_table
      : {};
  const values = attributes.map((attribute) => {
    const key = String(attribute.key || "");
    const profileValue = profile[`stat_${key}`];
    const value = hasCharacterCardValue(raw[key]) ? raw[key] : profileValue;
    const modifier = hasCharacterCardValue(modifiers[key])
      ? modifiers[key]
      : hasCharacterCardValue(value)
        ? table[String(value)]
        : null;
    return {
      key,
      label: labels[key] || attribute.label || key,
      value,
      modifier,
      minimum: attribute.minimum,
      maximum: attribute.maximum,
    };
  });
  const used = values.reduce(
    (sum, item) =>
      hasCharacterCardValue(item.value) && Number.isFinite(Number(item.value))
        ? sum + Number(item.value)
        : sum,
    0,
  );
  const budget = Number(stats?.budget ?? definition.budget ?? 0);
  const generationSnapshot =
    stats?.stat_generation_snapshot &&
    typeof stats.stat_generation_snapshot === "object"
      ? stats.stat_generation_snapshot
      : profile?.stat_generation_snapshot &&
          typeof profile.stat_generation_snapshot === "object"
        ? profile.stat_generation_snapshot
        : {};
  const generationSources = Array.isArray(generationSnapshot.sources)
    ? generationSnapshot.sources
    : [];
  if (!values.length) {
    return '<div class="empty-inline">当前模板没有数值属性。</div>';
  }
  return `
    <div class="character-card-budget">
      <span>属性预算</span>
      <strong>${escapeHTML(used)} / ${escapeHTML(budget)}</strong>
    </div>
    <div class="character-card-stats">
      ${values
        .map((item) => {
          const modifierNumber = Number(item.modifier);
          const modifierLabel = Number.isFinite(modifierNumber)
            ? `${modifierNumber >= 0 ? "+" : ""}${modifierNumber}`
            : "—";
          return `
            <div class="character-card-stat">
              <span>${escapeHTML(item.label)}</span>
              <strong>${
                hasCharacterCardValue(item.value)
                  ? escapeHTML(item.value)
                  : "—"
              }</strong>
              <small>修正 ${escapeHTML(modifierLabel)} · ${escapeHTML(
                item.minimum,
              )}—${escapeHTML(item.maximum)}</small>
            </div>
          `;
        })
        .join("")}
    </div>
    ${
      generationSources.length
        ? `<div class="character-card-note"><strong>属性来源</strong><span>${generationSources
            .map((source) => {
              const bonuses = Object.entries(source.stat_bonus || {})
                .map(([key, value]) => `${labels[key] || key}${Number(value) >= 0 ? "+" : ""}${value}`)
                .join("、");
              return `${source.option_label || source.option_id || source.source_id}：${bonuses}`;
            })
            .map((item) => escapeHTML(item))
            .join("；")}</span></div>`
        : ""
    }
  `;
}

function wsObjectLabel(item) {
  // A13：对象项取可读展示字段，避免输出 [object Object]。
  if (item && typeof item === "object" && !Array.isArray(item)) {
    return String(
      item.name ?? item.label ?? item.status ?? item.key ?? item.text ?? "",
    ).trim();
  }
  return String(item ?? "").trim();
}

function renderStatusChips(value) {
  // A9：把「当前状态」等列表型运行时字段渲染为可视化状态芯片。
  let items = [];
  if (Array.isArray(value)) {
    items = value.map(wsObjectLabel).filter(Boolean);
  } else if (typeof value === "object" && value !== null) {
    items = Object.entries(value)
      .filter(([, enabled]) => enabled)
      .map(([key]) => key);
  } else {
    items = String(value ?? "")
      .split(/[、,，;；/]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  const severity = (text) => {
    if (/中毒|流血|昏迷|重伤|残废|恐惧|诅咒|濒死/.test(text)) return "danger";
    if (/增益|祝福|专注|潜行|蓄力|鼓舞/.test(text)) return "ok";
    if (/减益|虚弱|疲劳|灼烧|冰冻|束缚|失明|沉默/.test(text)) return "warn";
    return "info";
  };
  return items.length
    ? `<div class="status-chip-list">${items
        .map(
          (item) =>
            `<span class="status-chip ${severity(item)}">${escapeHTML(
              item,
            )}</span>`,
        )
        .join("")}</div>`
    : `<span class="character-card-empty">未记录</span>`;
}

function renderCharacterRuntimeState(item) {
  const state =
    item.runtime_state && typeof item.runtime_state === "object"
      ? item.runtime_state
      : {};
  const keys = [
    ...Object.keys(CHARACTER_RUNTIME_LABELS),
    ...Object.keys(state).filter((key) => !(key in CHARACTER_RUNTIME_LABELS)),
  ];
  return keys
    .map(
      (key) => `
        <article class="character-card-field${characterCardFieldClass(state[key])}">
          <div class="character-card-field-label">
            <span>${escapeHTML(CHARACTER_RUNTIME_LABELS[key] || key)}</span>
          </div>
          <div class="character-card-field-value">
            ${
              key === "statuses"
                ? renderStatusChips(state[key])
                : characterCardValueHTML(state[key], "未记录")
            }
          </div>
        </article>
      `,
    )
    .join("");
}

function renderRosterCharacterCard(item, template, session, readonly, index) {
  const profile = characterCardProfile(item);
  const stats = item.card_stats || {};
  const attributes = Array.isArray(template.stats?.attributes)
    ? template.stats.attributes
    : [];
  const statPreview = attributes
    .map((attribute) => {
      const key = String(attribute.key || "");
      const value =
        stats.raw?.[key] ?? profile[`stat_${key}`] ?? attribute.default ?? "—";
      return `<span>${escapeHTML(attribute.label || key)} ${escapeHTML(value)}</span>`;
    })
    .join("");
  const cardVersion =
    item.card_version_no === null || item.card_version_no === undefined
      ? "尚未生成"
      : `v${item.card_version_no}`;
  const templateVersion =
    item.card_template_version ??
    item.draft_template_version ??
    template.version ??
    "—";
  const draftProgress =
    item.card_status === "draft" || item.card_status === "uncreated"
      ? `${Number(item.draft_step || 0)} / ${template.fields?.length || 0}`
      : "已提交";
  const disabled = readonly ? "disabled" : "";
  const actionButtons = `
    ${
      item.card_status === "approved"
        ? `<button class="action-button"
            data-session-detail-action="request-card-revision"
            data-ref="${escapeHTML(item.id)}" ${disabled}>新建修改版本</button>`
        : ""
    }
    ${
      item.card_status === "pending_review"
        ? `<button class="action-button"
            data-session-detail-action="card-approve"
            data-ref="${escapeHTML(item.id)}" ${disabled}>通过</button>
          <button class="action-button is-danger"
            data-session-detail-action="card-reject"
            data-ref="${escapeHTML(item.id)}" ${disabled}>拒绝</button>`
        : ""
    }
    ${
      item.participation_status === "active" && session.state === "running"
        ? `<button class="action-button"
            data-session-detail-action="designate"
            data-ref="${escapeHTML(item.id)}" ${disabled}>指定当前</button>`
        : ""
    }
    ${
      !["retired", "archived"].includes(item.participation_status)
        ? `<button class="action-button is-danger"
            data-session-detail-action="retire"
            data-ref="${escapeHTML(item.id)}" ${disabled}>安全退场</button>
          <button class="action-button is-danger"
            data-session-detail-action="ban"
            data-ref="${escapeHTML(item.id)}" ${disabled}>封禁</button>`
        : ""
    }
  `;
  const metaItems = [
    ["玩家昵称", item.display_name || "—"],
    ["群用户 ID", item.group_user_id || "—"],
    ["副本代号", item.character_code || "未设置"],
    ["角色卡版本", cardVersion],
    ["模板版本", `v${templateVersion}`],
    ["建卡进度", draftProgress],
    ["加入轮次", item.joined_round ?? "—"],
    ["连续超时", item.consecutive_timeouts ?? 0],
    ["运行状态修订", item.runtime_revision ? `r${item.runtime_revision}` : "—"],
    ["审核人", item.card_reviewed_by || "尚未审核"],
    ["角色卡生成", formatDate(item.card_version_created_at)],
    ["最后更新", formatDate(item.updated_at)],
  ];
  const opened = item.card_status === "pending_review" || index === 0;
  return `
    <details class="roster-character-card" ${opened ? "open" : ""}>
      <summary class="roster-character-summary">
        <div class="roster-character-identity">
          <div class="roster-character-title">
            <strong>${escapeHTML(item.character_name || item.display_name)}</strong>
            <span>${escapeHTML(item.character_code || "未设置代号")}</span>
            <!-- 0.12.0-A7：角色小卡直接改名 -->
            <button class="icon-btn roster-rename-btn"
              data-session-detail-action="rename-card"
              data-ref="${escapeHTML(item.id)}"
              title="修改角色名" aria-label="修改角色名"
              ${readonly ? "disabled" : ""}>✎</button>
          </div>
          <div class="roster-character-badges">
            <span class="character-card-badge status-${escapeHTML(
              item.card_status,
            )}">${escapeHTML(
              CHARACTER_CARD_STATUS_LABELS[item.card_status] || item.card_status,
            )}</span>
            <span class="character-card-badge">${escapeHTML(
              item.ready ? "已准备" : "未准备",
            )}</span>
            <span class="character-card-badge">${escapeHTML(
              PARTICIPANT_STATUS_LABELS[item.participation_status] ||
                item.participation_status,
            )}</span>
            <!-- 0.12.0-A6：GM 直接代改他人角色卡（走 sessions/card-revisions 修订机制） -->
            <button class="action-button" data-session-detail-action="edit-card"
              data-ref="${escapeHTML(item.id)}" ${readonly ? "disabled" : ""}>编辑角色卡</button>
          </div>
        </div>
        <div class="roster-character-stat-preview">${statPreview}</div>
        <span class="roster-character-toggle">完整角色卡</span>
      </summary>
      <div class="roster-character-body">
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>📇 CHARACTER PROFILE</span><h3>完整角色资料</h3></div>
            <small>按照本副本创建时锁定的角色卡模板展示</small>
          </div>
          <div class="character-card-fields">
            ${renderCharacterCardFields(profile, template)}
          </div>
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>📊 ATTRIBUTES</span><h3>属性与检定修正</h3></div>
            <small>审核和游戏裁定使用这里的持久化数值</small>
          </div>
          ${renderCharacterCardStats(profile, stats, template)}
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>⚡ INSTANCE STATE</span><h3>当前副本动态状态</h3></div>
            <small>角色基础卡不变；这些参数会随剧情推进更新</small>
          </div>
          <div class="character-card-fields is-runtime">
            ${renderCharacterRuntimeState(item)}
          </div>
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>🧾 CARD RECORD</span><h3>角色卡记录</h3></div>
            <small>审核、版本和席位信息</small>
          </div>
          <div class="character-card-records">
            ${metaItems
              .map(
                ([label, value]) => `
                  <div><span>${escapeHTML(label)}</span><strong>${escapeHTML(
                    value,
                  )}</strong></div>
                `,
              )
              .join("")}
          </div>
          ${
            item.aliases?.length
              ? `<div class="character-card-note"><strong>角色别名</strong><span>${escapeHTML(
                  item.aliases.join("、"),
                )}</span></div>`
              : ""
          }
          ${
            item.card_review_note
              ? `<div class="character-card-note"><strong>审核备注</strong><span>${escapeHTML(
                  item.card_review_note,
                )}</span></div>`
              : ""
          }
          ${
            item.exit_reason
              ? `<div class="character-card-note is-warning"><strong>退场原因</strong><span>${escapeHTML(
                  item.exit_reason,
                )}</span></div>`
              : ""
          }
        </section>
        ${
          actionButtons.trim()
            ? `<div class="roster-character-actions">${actionButtons}</div>`
            : ""
        }
      </div>
    </details>
  `;
}

function toast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = String(message);
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3800);
}

async function withBusy(button, action) {
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
  }
  try {
    return await action();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function showError(error) {
  console.error(error);
  toast(error?.message || String(error), "error");
}

function splitLines(value) {
  return String(value || "")
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter((item, index, list) => item && list.indexOf(item) === index);
}

const DURATION_UNITS = {
  seconds: { label: "秒", multiplier: 1 },
  minutes: { label: "分钟", multiplier: 60 },
  hours: { label: "小时", multiplier: 3600 },
  days: { label: "天", multiplier: 86400 },
};

const GLOBAL_TIME_FIELDS = {
  "#gtime-card-code": "card_code_ttl_seconds",
  "#gtime-card-draft": "card_draft_ttl_seconds",
  "#gtime-card-completion": "card_completion_timeout_seconds",
  "#gtime-preparation": "preparation_timeout_seconds",
  "#gtime-ready": "ready_timeout_seconds",
  "#gtime-turn": "turn_timeout_seconds",
  "#gtime-turn-reminder": "turn_reminder_seconds",
  "#gtime-standby": "standby_timeout_seconds",
  "#gtime-delegation": "delegation_ttl_seconds",
  "#gtime-all-idle": "all_idle_pause_seconds",
  "#gtime-vote-one": "vote_round_one_seconds",
  "#gtime-vote-two": "vote_round_two_seconds",
  "#gtime-vote-reminder": "vote_reminder_seconds",
};

const SESSION_TIME_FIELDS = {
  "#t-card-code": "card_code_ttl_seconds",
  "#t-card-draft": "card_draft_ttl_seconds",
  "#t-card-completion": "card_completion_timeout_seconds",
  "#t-preparation": "preparation_timeout_seconds",
  "#t-ready": "ready_timeout_seconds",
  "#t-turn": "turn_timeout_seconds",
  "#t-turn-reminder": "turn_reminder_seconds",
  "#t-standby": "standby_timeout_seconds",
  "#t-delegation": "delegation_ttl_seconds",
  "#t-all-idle": "all_idle_pause_seconds",
  "#t-vote-one": "vote_round_one_seconds",
  "#t-vote-two": "vote_round_two_seconds",
  "#t-vote-reminder": "vote_reminder_seconds",
};

function bestDurationUnit(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "minutes";
  if (value % 86400 === 0) return "days";
  if (value % 3600 === 0) return "hours";
  if (value % 60 === 0) return "minutes";
  return "seconds";
}

function ensureDurationControl(selector) {
  const input = $(selector);
  if (!input) return null;
  const existing = input.closest(".duration-control");
  if (existing) return existing;
  const raw = input.value.trim();
  const seconds = raw === "" || raw === "-1" ? null : Number(raw);
  const unitName = bestDurationUnit(seconds);
  const unit = DURATION_UNITS[unitName];
  const wrapper = document.createElement("div");
  wrapper.className = "duration-control";
  wrapper.innerHTML = `
    <select class="duration-unit" aria-label="时间单位">
      ${Object.entries(DURATION_UNITS)
        .map(
          ([key, item]) =>
            `<option value="${key}" ${key === unitName ? "selected" : ""}>${item.label}</option>`,
        )
        .join("")}
    </select>
    <label class="duration-unlimited">
      <input type="checkbox" ${seconds === null ? "checked" : ""} />
      <span>不限时</span>
    </label>
  `;
  input.replaceWith(wrapper);
  input.min = "1";
  input.step = "1";
  input.value = seconds === null ? "" : String(seconds / unit.multiplier);
  wrapper.prepend(input);
  const unlimited = wrapper.querySelector(".duration-unlimited input");
  const refresh = () => {
    input.disabled = unlimited.checked;
    wrapper.querySelector(".duration-unit").disabled = unlimited.checked;
  };
  unlimited.addEventListener("change", refresh);
  refresh();
  return wrapper;
}

function setTimeValue(selector, seconds) {
  const control = ensureDurationControl(selector);
  if (!control) return;
  const input = control.querySelector('input[type="number"]');
  const select = control.querySelector(".duration-unit");
  const unlimited = control.querySelector(".duration-unlimited input");
  const normalized =
    seconds === null || seconds === undefined || Number(seconds) === -1
      ? null
      : Number(seconds);
  const unitName = bestDurationUnit(normalized);
  select.value = unitName;
  unlimited.checked = normalized === null;
  input.value =
    normalized === null
      ? ""
      : String(normalized / DURATION_UNITS[unitName].multiplier);
  input.disabled = unlimited.checked;
  select.disabled = unlimited.checked;
}

function readTimeValue(selector) {
  const control = ensureDurationControl(selector);
  if (!control) return null;
  if (control.querySelector(".duration-unlimited input").checked) return null;
  const input = control.querySelector('input[type="number"]');
  const value = Number(input.value);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error("时间数值必须大于 0，或勾选不限时");
  }
  const unitName = control.querySelector(".duration-unit").value;
  return Math.round(value * DURATION_UNITS[unitName].multiplier);
}

function applyTimePreset(name) {
  const presets = {
    instant: {
      card_code_ttl_seconds: 900,
      card_draft_ttl_seconds: 86400,
      card_completion_timeout_seconds: 3600,
      preparation_timeout_seconds: 7200,
      ready_timeout_seconds: 600,
      turn_timeout_seconds: 180,
      turn_reminder_seconds: 60,
      standby_timeout_seconds: 86400,
      delegation_ttl_seconds: 7200,
      all_idle_pause_seconds: 600,
      vote_round_one_seconds: 180,
      vote_round_two_seconds: 120,
      vote_reminder_seconds: 60,
      max_consecutive_timeouts: 2,
    },
    slow: {
      card_code_ttl_seconds: 1800,
      card_draft_ttl_seconds: 604800,
      card_completion_timeout_seconds: 86400,
      preparation_timeout_seconds: 86400,
      ready_timeout_seconds: 1800,
      turn_timeout_seconds: 600,
      turn_reminder_seconds: 180,
      standby_timeout_seconds: 604800,
      delegation_ttl_seconds: 86400,
      all_idle_pause_seconds: 600,
      vote_round_one_seconds: 600,
      vote_round_two_seconds: 300,
      vote_reminder_seconds: 120,
      max_consecutive_timeouts: 2,
    },
    manual: Object.fromEntries(
      Object.values(GLOBAL_TIME_FIELDS).map((key) => [key, null]),
    ),
  };
  presets.manual.max_consecutive_timeouts = -1;
  const preset = presets[name];
  if (!preset) return;
  Object.entries(GLOBAL_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, preset[key]),
  );
  $("#gtime-timeout-count").value = preset.max_consecutive_timeouts;
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
  toast(
    {
      instant: "已应用即时团时间预设",
      slow: "已应用慢速群团时间预设",
      manual: "已应用全手动推进预设",
    }[name],
    "success",
  );
}

function providerOptionHTML(selectedId, emptyLabel) {
  const known = new Set(app.providers.map((item) => item.id));
  const options = [
    `<option value="">${escapeHTML(emptyLabel)}</option>`,
    ...app.providers.map((item) => {
      const detail = [item.name, item.model].filter(Boolean).join(" · ");
      return `<option value="${escapeHTML(item.id)}" ${
        item.id === selectedId ? "selected" : ""
      }>${escapeHTML(detail || item.id)}（${escapeHTML(item.id)}）</option>`;
    }),
  ];
  if (selectedId && !known.has(selectedId)) {
    options.push(
      `<option value="${escapeHTML(selectedId)}" selected>${escapeHTML(
        selectedId,
      )}（当前未加载）</option>`,
    );
  }
  return options.join("");
}

function renderFallbackProviders(values = null) {
  const configured = Array.isArray(values)
    ? values
    : [...$("#fallback-provider-list").querySelectorAll("select")].map(
        (item) => item.value,
      );
  const root = $("#fallback-provider-list");
  root.innerHTML = configured.length
    ? configured
        .map(
          (providerId, index) => `
            <div class="provider-fallback-row">
              <span class="provider-order">${index + 1}</span>
              <select data-fallback-provider>
                ${providerOptionHTML(providerId, "请选择备用模型")}
              </select>
              <div class="table-actions">
                <button type="button" class="action-button"
                  data-fallback-action="up" data-index="${index}" ${
                    index === 0 ? "disabled" : ""
                  }>上移</button>
                <button type="button" class="action-button"
                  data-fallback-action="down" data-index="${index}" ${
                    index === configured.length - 1 ? "disabled" : ""
                  }>下移</button>
                <button type="button" class="action-button is-danger"
                  data-fallback-action="remove" data-index="${index}">移除</button>
              </div>
            </div>
          `,
        )
        .join("")
    : '<div class="empty-inline">未设置备用模型；主模型失败时本轮会停止。</div>';
}

// A13 热修复：AstrBot 插件页运行在沙箱 iframe 中，window.localStorage 访问可能抛
// SecurityError；所有本地存储读写统一经 safeLocalStorage()（不可用时返回 null）。
function safeLocalStorage() {
  try {
    return window.localStorage;
  } catch (_) {
    return null;
  }
}

function applyBridgeContext(context) {
  const value = context || {};
  // A14（审计 F9）：本地主题偏好优先于 AstrBot 上下文，避免每次打开被重置。
  const savedTheme = safeLocalStorage()?.getItem("tavern_theme");
  if (savedTheme === "dark" || savedTheme === "light") {
    document.documentElement.dataset.theme = savedTheme;
  } else {
    document.documentElement.dataset.theme = value.isDark ? "dark" : "light";
  }
  if (value.locale) {
    document.documentElement.lang = value.locale;
  }
  document.title = bridge.t("pages.console.title", "酒馆控制台");
  syncThemeIcon();
}

// 0.12.0-A3：浅色/深色主题手动切换（默认跟随 AstrBot isDark 上下文）。
function syncThemeIcon() {
  const button = $("#theme-toggle");
  if (!button) return;
  const dark = document.documentElement.dataset.theme === "dark";
  button.textContent = dark ? "☾" : "☀";
  button.setAttribute(
    "aria-label",
    dark ? "切换到浅色主题" : "切换到深色主题",
  );
}
(function bindThemeToggle() {
  const button = $("#theme-toggle");
  if (!button) return;
  // A14（审计 F9）：主题偏好持久化；优先取本地保存值，其次跟随 AstrBot 上下文。
  const savedTheme = safeLocalStorage()?.getItem("tavern_theme");
  if (savedTheme === "dark" || savedTheme === "light") {
    document.documentElement.dataset.theme = savedTheme;
  }
  button.addEventListener("click", () => {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      safeLocalStorage()?.setItem("tavern_theme", next);
    } catch (_) {
      // 不可用时静默跳过（与向导草稿一致）。
    }
    syncThemeIcon();
  });
  syncThemeIcon();
})();

function switchView(name) {
  if (!viewMeta[name]) return;
  app.view = name;
  $$(".nav-item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.view === name);
  });
  $$(".view").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === name);
  });
  const [kicker, title] = viewMeta[name];
  $("#page-kicker").textContent = kicker;
  $("#page-title").textContent = title;
  $("#sidebar").classList.remove("is-open");
  if (name === "memories") loadMemories().catch(showError);
  if (name === "audit") loadAudit().catch(showError);
  if (name === "todo") loadDeliveries().catch(showError);
  if (name === "about") renderAbout();
  // v0.12.0：副本实时仪表盘（进入视图时加载 + 每 5 秒自动刷新；离开即停）。
  if (name === "session_detail") {
    loadSessionDashboard().catch(showError);
    if (liveState.timer) clearInterval(liveState.timer);
    liveState.timer = setInterval(() => {
      if ($("#live-session-picker")?.value) {
        // 0.12.0-A3（#3）：轮询只做倒计时小窗口的轻量局部刷新。
        refreshLiveTimers();
      }
    }, 5000);
  } else if (liveState.timer) {
    clearInterval(liveState.timer);
    liveState.timer = null;
  }
  if (name === "market") loadMarket().catch(showError);
}

function setConnection(state, text) {
  const pill = $("#live-pill");
  pill.classList.toggle("is-live", state === "live");
  pill.classList.toggle("is-error", state === "error");
  $("#live-text").textContent = text;
  $("#side-health").textContent = text;
  const dot = $("#side-health-dot");
  dot.classList.toggle("is-ok", state === "live");
  dot.classList.toggle("is-error", state === "error");
}

function applySessionResponse(sessions) {
  app.sessions = sessions.items || [];
  app.sessionOptions = sessions.options || app.sessionOptions || [];
  app.sessionGroups = sessions.groups || [];
  app.sessionTotal = Number(sessions.total || 0);
  app.sessionPages = Math.max(1, Number(sessions.pages || 1));
  app.sessionPage = Math.min(
    app.sessionPages,
    Math.max(1, Number(sessions.page || app.sessionPage)),
  );
}

async function loadSessionPage() {
  const sessions = await bridge.apiGet("sessions", {
    q: app.sessionQuery,
    scope: app.sessionScope,
    page: app.sessionPage,
    page_size: app.sessionPageSize,
  });
  applySessionResponse(sessions);
  renderSessions();
  renderSessionSelects();
}

async function loadCore() {
  const [overview, worlds, sessions, settings, providers] = await Promise.all([
    bridge.apiGet("overview"),
    bridge.apiGet("worlds", {
      include_archived: $("#show-archived-worlds").checked,
    }),
    bridge.apiGet("sessions", {
      q: app.sessionQuery,
      scope: app.sessionScope,
      page: app.sessionPage,
      page_size: app.sessionPageSize,
    }),
    bridge.apiGet("settings"),
    bridge.apiGet("providers"),
  ]);
  app.overview = overview;
  app.worlds = worlds.items || [];
  applySessionResponse(sessions);
  app.settings = settings.settings;
  app.configState = settings.config_state || null;
  app.providerHealth = settings.provider_health || [];
  app.providers = providers.items || [];
  renderOverview();
  renderWorlds();
  renderSessions();
  renderSessionSelects();
  renderSettings();
  setConnection("live", "运行正常");
}

function renderOverview() {
  const data = app.overview;
  if (!data) return;
  const counts = data.counts || {};
  const tokenUsage = data.token_usage || {};
  const cards = [
    ["运行会话", counts.running || 0, `${counts.sessions || 0} 个已建会话`, "◉"],
    ["世界包", counts.worlds || 0, "可加载世界", "◇"],
    ["长期记忆", counts.memories || 0, "跨回合事实", "≋"],
    ["Token 用量", formatTokens(tokenUsage.used), `${counts.snapshots || 0} 个恢复点 · 限额 ${formatTokens(tokenUsage.limit)}`, "◔"],
  ];
  $("#metrics").innerHTML = cards
    .map(
      ([label, value, note, icon]) => `
        <article class="metric-card">
          <div class="metric-icon">${icon}</div>
          <div class="metric-label">${escapeHTML(label)}</div>
          <div class="metric-value">${escapeHTML(value)}</div>
          <div class="metric-note">${escapeHTML(note)}</div>
        </article>
      `,
    )
    .join("");

  const sessions = data.sessions || [];
  const active = sessions.filter((item) => item.state !== "closed").slice(0, 5);
  // A9：总览「正在发生」改为玻璃卡片 + 六格信息（副本包 / 群号 / 当前位置 /
  // 当前章节 / 剧情回合 / 当前行动者）+ 「详情 / 实时」入口按钮。
  $("#dashboard-sessions").innerHTML = active.length
    ? active
        .map(
          (session) => `
            <article class="live-session-card">
              <div class="ls-head">
                <div class="ls-main">
                  <div class="ls-title">${escapeHTML(
                    session.instance_name || session.world_name,
                  )}</div>
                  <div class="ls-meta">${escapeHTML(
                    session.world_name,
                  )} · ${escapeHTML(session.platform_id)} · 群 ${escapeHTML(
                    session.group_id,
                  )}</div>
                </div>
                ${statusBadge(session.state)}
              </div>
              <div class="ls-facts">
                <div class="ls-fact">
                  <span>副本包</span>
                  <strong>${escapeHTML(
                    session.instance_name || session.world_name,
                  )}</strong>
                </div>
                <div class="ls-fact">
                  <span>群号</span>
                  <strong>${escapeHTML(session.group_id)}</strong>
                </div>
                <div class="ls-fact">
                  <span>当前位置</span>
                  <strong>${escapeHTML(
                    session.world_state?.location || "地点未记录",
                  )}</strong>
                </div>
                <div class="ls-fact">
                  <span>当前章节</span>
                  <strong>${escapeHTML(
                    session.world_state?.progress?.chapter || "章节未设置",
                  )}</strong>
                </div>
                <div class="ls-fact">
                  <span>剧情回合</span>
                  <strong>第 ${escapeHTML(session.turn_no)} 轮</strong>
                </div>
                <div class="ls-fact">
                  <span>当前行动者</span>
                  <strong>${escapeHTML(
                    session.current_name || waitingLabel(session),
                  )}</strong>
                </div>
              </div>
              <div class="ls-actions">
                <button class="action-button" type="button"
                  data-open-session="${escapeHTML(
                    session.id,
                  )}" title="打开 SESSION INSPECTOR">详情</button>
                <button class="action-button" type="button"
                  data-session-live="${escapeHTML(
                    session.id,
                  )}" title="打开副本实时仪表盘">实时</button>
              </div>
            </article>
          `,
        )
        .join("")
    : `
      <div class="empty-state compact">
        <div class="empty-symbol">◌</div>
        <span>当前没有运行中的群会话</span>
      </div>
    `;

  const security = data.security || {};
  const whitelistRequired = security.whitelist_required !== false;
  const groupBoundaryReady =
    !whitelistRequired || security.allowed_group_count > 0;
  const integrity = [
    [
      data.database_ok,
      "SQLite 完整性",
      data.database_ok ? "快速校验通过" : "需要检查数据库",
    ],
    [
      security.admin_count > 0,
      "管理员身份",
      `${security.admin_count || 0} 个授权 ID`,
    ],
    [
      groupBoundaryReady,
      "群聊边界",
      !whitelistRequired
        ? "白名单未启用"
        : `${security.allowed_group_count || 0} 个允许群`,
    ],
    [true, "Schema 版本", `v${data.schema_version} · 插件 v${data.plugin_version}`],
  ];
  const integrityBlock = data.integrity || {};
  if (integrityBlock.schema_version !== undefined) {
    integrity.push([
      (integrityBlock.storage_errors || 0) === 0,
      "存储同步",
      (integrityBlock.storage_errors || 0) > 0
        ? `${integrityBlock.storage_errors} 个异常`
        : "正常",
    ]);
    integrity.push([
      (integrityBlock.invalid_transitions_24h || 0) === 0,
      "状态迁移",
      `24h 非法迁移 ${integrityBlock.invalid_transitions_24h || 0} 次`,
    ]);
    integrity.push([
      true,
      "恢复点",
      `${integrityBlock.recovery_points ?? counts.snapshots ?? 0} 个快照`,
    ]);
  }
  $("#integrity-list").innerHTML = integrity
    .map(
      ([ok, title, note]) => `
        <div class="integrity-item">
          <span class="integrity-icon ${ok ? "ok" : "warn"}">${ok ? "✓" : "!"}</span>
          <div class="integrity-body">
            <div class="integrity-title">${escapeHTML(title)}</div>
            <div class="integrity-note">${escapeHTML(note)}</div>
          </div>
        </div>
      `,
    )
    .join("");

  const checklist = [
    [security.admin_count > 0, "设置真实管理员 ID", "群内权限不依赖昵称或文本声明"],
    [
      groupBoundaryReady,
      !whitelistRequired
        ? "群白名单已关闭"
        : security.allowed_group_count > 0
          ? "已绑定允许群"
          : "等待绑定首个群",
      !whitelistRequired
        ? "所有群都可进入会话，管理命令仍仅限授权 ID"
        : security.allowed_group_count > 0
          ? "仅允许列表中的群进入酒馆"
          : "授权管理员发送 /酒馆 开启 后自动绑定并选择副本",
    ],
    [counts.worlds > 0, "准备至少一个世界包", "默认已附带阿尔维恩：灰烬王冠"],
    [
      true,
      "设置叙事模型链",
      `${app.settings?.model?.fallback_provider_ids?.length || 0} 个备用模型 · ${
        app.settings?.model?.image_caption_provider_id
          ? "图片转述已配置"
          : "图片转述未配置"
      }`,
    ],
  ];
  const readiness = data.readiness || {};
  if (readiness.admin_ready !== undefined) {
    checklist.push([
      readiness.providers_ready === true,
      "模型链可用",
      readiness.providers_ready
        ? "主备模型可用"
        : "等待模型健康检查",
    ]);
    checklist.push([
      (readiness.worlds_ready || 0) > 0,
      "世界包就绪",
      `${readiness.worlds_ready || 0} 个世界通过体检`,
    ]);
    checklist.push([
      true,
      "建卡码机制",
      `TTL ${readiness.card_code_ttl_seconds ?? 1800}s`,
    ]);
  }
  $("#readiness-checklist").innerHTML = checklist
    .map(
      ([ok, title, note]) => `
        <div class="integrity-item">
          <span class="integrity-icon ${ok ? "ok" : "warn"}">${ok ? "✓" : "!"}</span>
          <div class="integrity-body">
            <div class="integrity-title">${escapeHTML(title)}</div>
            <div class="integrity-note">${escapeHTML(note)}</div>
          </div>
        </div>
      `,
    )
    .join("");

  $("#security-notice").classList.toggle("is-hidden", security.ready);
  if (!security.ready) {
    const missing = [];
    if (!security.admin_count) missing.push("管理员 ID");
    $("#security-notice-text").textContent =
      `仍缺少：${missing.join("、")}。填写后，由授权管理员在目标群发送 /酒馆 开启即可绑定并选择副本。`;
  }

  // 0.12.0-A3：关键数据概览（可折叠）。0.12.0-A5：改为卡片化展示。
  const keydata = [
    ["世界包", counts.worlds || 0],
    ["故事副本", counts.sessions || 0],
    ["运行中", counts.running || 0],
    ["准备中", counts.preparing || 0],
    ["参与玩家", counts.players || 0],
    ["长期记忆", counts.memories || 0],
    ["快速恢复点", counts.snapshots || 0],
    ["进行中投票", counts.open_votes || 0],
    ["活跃倒计时", counts.active_timers || 0],
    ["Token 用量", formatTokens(tokenUsage.used)],
  ];
  $("#keydata-meta").textContent = `${keydata.length} 项指标`;
  const keydataRoot = $("#keydata-table");
  if (keydataRoot) {
    keydataRoot.className = "keydata-grid";
    keydataRoot.innerHTML = keydata
      .map(
        ([label, value]) => `
          <div class="keydata-tile">
            <span>${escapeHTML(label)}</span>
            <strong>${escapeHTML(String(value))}</strong>
          </div>
        `,
      )
      .join("");
  }

  // 0.12.0-A3：群内指令统计条。
  const commands = data.commands || {};
  if (!$("#command-stats")) {
    $(".command-reference .command-list")?.insertAdjacentHTML(
      "beforebegin",
      '<div class="command-stats" id="command-stats"></div>',
    );
  }
  const commandStats = $("#command-stats");
  if (commandStats) {
    commandStats.innerHTML = [
      ["24h 操作", commands.command_count_24h ?? "—"],
      [
        "成功率",
        commands.success_rate !== undefined
          ? `${commands.success_rate}%`
          : "—",
      ],
      ["兜底解析", commands.relaxed_parse_hits_24h ?? 0],
    ]
      .map(
        ([label, value]) =>
          `<span class="cs-item"><em>${escapeHTML(
            label,
          )}</em><b>${escapeHTML(String(value))}</b></span>`,
      )
      .join("");
  }

  // 0.12.0-A3：待办导航徽标。
  const todoBadge = $("#todo-badge");
  if (todoBadge) {
    const todoCount =
      (counts.open_votes || 0) + (counts.active_timers || 0) +
      (counts.pending_deliveries || 0);
    todoBadge.textContent = String(todoCount);
    todoBadge.hidden = todoCount === 0;
  }
}

// ── 0.12.0-A3：总览辅助与新板块（待办 / 关于插件） ────────────────────
function formatTokens(value) {
  const number = Number(value || 0);
  if (Number.isNaN(number)) return "—";
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `${Math.round(number / 1_000)}k`;
  return String(number);
}

function waitingLabel(session) {
  return (
    {
      vote: "等待集体投票",
      choice: "等待个人选择",
      preparation: "等待建卡",
      admin: "等待管理员",
    }[session?.waiting_for] || "进行中"
  );
}

function renderTodo() {
  const data = app.overview;
  const root = $("#todo-root");
  if (!data || !root) return;
  const counts = data.counts || {};
  const sessions = (data.sessions || []).filter(
    (session) => session.state !== "closed",
  );
  const section = (title, count, items) => `
    <div class="todo-group">
      <h3>${escapeHTML(title)} <span class="todo-count">${escapeHTML(
        String(count),
      )}</span></h3>
      ${
        items.length
          ? items
              .map(
                (session) => `
                  <div class="todo-row">
                    <div class="todo-main">
                      <div class="todo-title">${escapeHTML(
                        session.instance_name || session.world_name,
                      )}</div>
                      <div class="todo-sub">${escapeHTML(
                        waitingLabel(session),
                      )}${
                        session.world_state?.progress?.chapter
                          ? ` · ${escapeHTML(
                              session.world_state.progress.chapter,
                            )}`
                          : ""
                      }</div>
                    </div>
                    <button class="button button-small"
                      data-open-session="${escapeHTML(session.id)}">打开</button>
                  </div>
                `,
              )
              .join("")
          : '<div class="empty-state compact"><span>无</span></div>'
      }
    </div>
  `;
  const deliveries = app.deliveries || [];
  const deliveryRows = deliveries.length
    ? deliveries.map((item) => `
        <div class="todo-row delivery-row">
          <div class="todo-main">
            <div class="todo-title">${escapeHTML(item.kind || "文本通知")}</div>
            <div class="todo-sub">${escapeHTML(item.text || "")}</div>
            <small>${escapeHTML(item.last_error || "等待下次会话消息自动补发")} · 已尝试 ${escapeHTML(item.attempts || 0)} 次</small>
          </div>
          <div class="table-actions">
            <button class="action-button" data-delivery-action="retry" data-delivery-id="${escapeHTML(item.id)}" title="立即尝试主动发送；平台仍不支持时会继续保留">重试</button>
            <button class="action-button is-danger" data-delivery-action="dismiss" data-delivery-id="${escapeHTML(item.id)}" title="仅移除这条通知，不改变已经提交的剧情状态">忽略</button>
          </div>
        </div>`).join("")
    : '<div class="empty-state compact"><span>没有等待投递的通知</span></div>';
  root.innerHTML =
    section(
      "进行中投票",
      counts.open_votes || 0,
      sessions.filter((session) => session.waiting_for === "vote"),
    ) +
    section(
      "等待建卡",
      counts.preparing || 0,
      sessions.filter((session) => session.waiting_for === "preparation"),
    ) +
    section(
      "运行中副本",
      counts.running || 0,
      sessions.filter((session) => session.state === "running"),
    ) +
    `<div class="todo-group">
      <h3>活跃倒计时 <span class="todo-count">${escapeHTML(
        String(counts.active_timers || 0),
      )}</span></h3>
      <div class="empty-state compact">
        <span>计时器详情请打开对应副本的实时仪表盘</span>
      </div>
    </div>
    <div class="todo-group">
      <h3>待投递通知 <span class="todo-count">${escapeHTML(String(deliveries.length))}</span></h3>
      <p class="field-hint">平台无法主动推送时，通知会持久化并在该会话下一次收到消息时自动补发。重试和忽略均需要副本 DM 或管理员权限。</p>
      ${deliveryRows}
    </div>`;
}

async function loadDeliveries() {
  const response = await bridge.apiGet("deliveries", { status: "pending", limit: 100 });
  app.deliveries = response.items || [];
  renderTodo();
}

const ABOUT_FEATURES = [
  ["◈", "世界协议 v5", "声明式能力与交互规则、稳定世界编号；导入前强制体检。"],
  ["⚄", "确定性检定", "两阶段检定 + 幂等凭证落库，骰面可回放、可审计。"],
  ["☑", "集体表决", "全队行动投票、多数通过推进叙事，超时按实票判定。"],
  ["✎", "建卡审核", "私聊建卡码 + 预设数值模式；GM 审核后进入副本。"],
  ["◔", "真人 DM 模式", "主持人接管叙事，与 AI 主持可审计切换。"],
  ["⌁", "审计与恢复", "快照 / 自动回滚 / 命名存档 / 完整备份。"],
  ["⇄", "可靠通知", "主动推送失败进入持久化队列，下次会话消息自动补发。"],
  ["♧", "群聊体验", "世界包可选声明建卡、多人节奏、安全边界与连续性策略。"],
];

const ABOUT_MODULES = [
  ["◈", "叙事引擎", "剧情推进、回合秩序、模型调用与降级链。"],
  ["▤", "规则引擎", "实体注册 · 条件 · 操作 · 事件管线 · 裁定凭证。"],
  ["▦", "存储层", "SQLite Schema 12 + WAL，短事务、通知补偿与自动备份。"],
  ["◔", "实时监控", "副本仪表盘：行动者 / 倒计时 / 投票 / 时间线。"],
  ["☰", "控制台", "世界向导、市场、审计与脱敏诊断报告。"],
];

function renderAbout() {
  const data = app.overview || {};
  const root = $("#about-root");
  if (!root) return;
  root.innerHTML = `
    <div class="about-grid">
      <article class="panel">
        <div class="panel-head">
          <div><div class="eyebrow">VERSION</div><h2>版本与信息</h2></div>
        </div>
        <table class="keydata-table">
          <tr><td>插件版本</td><td class="num">${escapeHTML(
            data.plugin_version ? `v${data.plugin_version}` : "—",
          )}</td></tr>
          <tr><td>仓库</td><td class="num"><a href="https://github.com/horizoe10/astrbot_plugin_tavern" target="_blank" rel="noopener noreferrer">GitHub · astrbot_plugin_tavern</a></td></tr>
          <tr><td>数据库</td><td class="num">Schema ${escapeHTML(
            String(data.schema_version ?? "—"),
          )}</td></tr>
          <tr><td>数据库大小</td><td class="num">${formatBytes(
            data.database_size,
          )}</td></tr>
          <tr><td>世界协议</td><td class="num">v2 / v3 / v4 / v5</td></tr>
          <tr><td>角色卡模板</td><td class="num">v6</td></tr>
          <tr><td>AstrBot</td><td class="num">≥ 4.26 且 &lt; 5</td></tr>
        </table>
        <div class="panel-head" style="margin-top:18px;">
          <div><div class="eyebrow">FEATURES</div><h2>核心功能</h2></div>
        </div>
        <div class="about-features">
          ${ABOUT_FEATURES.map(
            ([icon, title, desc]) => `
              <div class="feature-tile">
                <span class="f-icon">${icon}</span>
                <strong>${escapeHTML(title)}</strong>
                <p>${escapeHTML(desc)}</p>
              </div>
            `,
          ).join("")}
        </div>
      </article>

      <article class="panel">
        <div class="panel-head">
          <div><div class="eyebrow">MODULES</div><h2>模块简介</h2></div>
        </div>
        <div class="about-modules">
          ${ABOUT_MODULES.map(
            ([icon, title, desc]) => `
              <div class="about-module">
                <span class="m-icon">${icon}</span>
                <div><strong>${escapeHTML(title)}</strong><p>${escapeHTML(
                  desc,
                )}</p></div>
              </div>
            `,
          ).join("")}
        </div>
        <div class="panel-head" style="margin-top:18px;">
          <div><div class="eyebrow">COMPATIBILITY</div><h2>兼容性</h2></div>
        </div>
        <div class="compat-grid">
          <div class="compat-tile">
            <span class="compat-icon">◇</span>
            <strong>全平台文本</strong>
            <p>核心流程只依赖 AstrBot 标准文本事件与稳定会话身份。</p>
          </div>
          <div class="compat-tile">
            <span class="compat-icon">▦</span>
            <strong>能力降级</strong>
            <p>主动推送、私聊与话题能力按适配器能力处理；失败不误报。</p>
          </div>
          <div class="compat-tile">
            <span class="compat-icon">⏭</span>
            <strong>普通群聊</strong>
            <p>始终旁路，不调用酒馆模型、不写入时间线。</p>
          </div>
        </div>
      </article>
    </div>
    <article class="panel" id="about-extensions" style="margin-top:16px">
      <div class="panel-head">
        <div><div class="eyebrow">EXTENSIONS & EVENTS</div><h2>扩展点与事件</h2></div>
        <span class="status-badge" id="about-extensions-meta">读取中…</span>
      </div>
      <div id="about-extensions-body" class="about-modules">
        <div class="empty-state compact"><span>正在读取扩展清单</span></div>
      </div>
    </article>`;
    loadAboutExtensions();
}

// A14：关于页「扩展点与事件」自描述清单（extensions / hooks / meta/capabilities）。
async function loadAboutExtensions() {
  const body = $("#about-extensions-body");
  const metaEl = $("#about-extensions-meta");
  if (!body) return;
  body.setAttribute("aria-busy", "true");
  body.innerHTML = `
    <div class="empty-state compact extension-loading" role="status">
      <span class="loading-dot" aria-hidden="true"></span>
      <strong>正在读取扩展菜单</strong>
      <span>正在检查运行时能力、扩展注册表与事件订阅。</span>
    </div>`;
  if (metaEl) metaEl.textContent = "读取中…";

  const endpoints = [
    ["运行时能力", "meta/capabilities"],
    ["扩展注册表", "extensions"],
    ["事件订阅", "hooks/events"],
  ];
  const settled = await Promise.allSettled(
    endpoints.map(([, path]) => bridge.apiGet(path)),
  );
  if (!body.isConnected) return;

  const values = settled.map((result) =>
    result.status === "fulfilled" && result.value && typeof result.value === "object"
      ? result.value
      : {},
  );
  const [caps, ext, hooks] = values;
  const failures = settled.flatMap((result, index) => {
    if (result.status === "rejected") {
      return [`${endpoints[index][0]}：${result.reason?.message || String(result.reason)}`];
    }
    const itemErrors = Array.isArray(result.value?.errors) ? result.value.errors : [];
    return itemErrors.map((message) => `${endpoints[index][0]}：${message}`);
  });
  const loadedCount = settled.filter((result) => result.status === "fulfilled").length;
  const retry = failures.length
    ? `<button class="action-button extension-retry" id="about-extensions-retry" type="button">重新读取</button>`
    : "";

  if (!loadedCount) {
    if (metaEl) metaEl.textContent = "读取失败";
    body.removeAttribute("aria-busy");
    body.innerHTML = `
      <div class="empty-state compact extension-error" role="alert">
        <strong>扩展菜单读取失败</strong>
        <span>${escapeHTML(failures.join("；") || "服务器没有返回可用数据")}</span>
        ${retry}
      </div>`;
    $("#about-extensions-retry")?.addEventListener("click", loadAboutExtensions);
    return;
  }

  if (metaEl) {
    metaEl.textContent = failures.length
      ? `${ext.total || 0} 扩展 · 部分数据异常`
      : `${ext.total || 0} 扩展 · ${hooks.subscribed_count || 0} 订阅`;
  }
  const kinds = Object.entries(ext.kinds || {})
    .map(
      ([kind, names]) =>
        `<div class="about-module"><span class="m-icon">🔌</span><div><strong>${escapeHTML(
          kind,
        )}</strong><p>${escapeHTML(Array.isArray(names) ? names.join("、") || "暂无注册项" : "数据不可用")}</p></div></div>`,
    )
    .join("");
  const extensionEmpty = kinds
    ? ""
    : '<div class="empty-state compact extension-empty"><strong>暂无已注册扩展</strong><span>内置能力仍可正常使用；安装扩展后会显示在这里。</span></div>';
  const warning = failures.length
    ? `<div class="template-preview-error extension-warning" role="alert"><strong>部分数据未能读取</strong><span>${escapeHTML(
        failures.join("；"),
      )}</span>${retry}</div>`
    : "";
  body.removeAttribute("aria-busy");
  body.innerHTML = `
    <div class="feature-tile"><strong>扩展类型</strong><span>${escapeHTML(
      (caps.extension_kinds || []).join(" / ") || "暂无能力数据",
    )}</span></div>
    <div class="feature-tile"><strong>操作类型</strong><span>${escapeHTML(
      String(caps.operation_types?.length || 0),
    )} 种声明式操作 · ${escapeHTML(
      (caps.persistence_scopes || []).join(" / ") || "暂无范围数据",
    )}</span></div>
    <div class="feature-tile"><strong>元素契约</strong><span>v${escapeHTML(
      caps.elemental_contract_version || "—",
    )} · ${escapeHTML((caps.resolution_modes || []).join(" / ") || "暂无解析模式")}</span></div>
    <div class="feature-tile"><strong>可订阅事件</strong><span>${escapeHTML(
      String(hooks.supported?.length || 0),
    )} 个 · ${escapeHTML(
      Object.keys(hooks.subscriptions || {}).join("、") || "暂无订阅",
    )}</span></div>
    ${warning}
    ${kinds}
    ${extensionEmpty}`;
  $("#about-extensions-retry")?.addEventListener("click", loadAboutExtensions);
}

function renderWorlds() {
  const root = $("#world-grid");
  if (!app.worlds.length) {
    root.innerHTML = `
      <div class="empty-state">
        <div class="empty-symbol">◇</div>
        <strong>还没有世界包</strong>
        <span>创建一个世界后才能建立群会话。</span>
      </div>
    `;
    return;
  }
  root.innerHTML = app.worlds
    .map(
      (world) => `
        <article class="world-card ${world.archived ? "is-archived" : ""}">
          <div class="world-card-top">
            <span class="world-number">WORLD ${String(world.display_no || 0).padStart(2, "0")}</span>
            ${world.archived ? '<span class="status-badge status-closed">已归档</span>' : ""}
            <!-- 0.12.0-A5：上移 / 下移 / 归档 收敛为右上角图标按钮 -->
            <div class="world-card-corner">
              <button class="icon-btn" data-world-action="up" data-id="${escapeHTML(
                world.id,
              )}" title="上移" aria-label="上移">↑</button>
              <button class="icon-btn" data-world-action="down" data-id="${escapeHTML(
                world.id,
              )}" title="下移" aria-label="下移">↓</button>
              ${
                world.archived
                  ? `<button class="icon-btn" data-world-action="restore" data-id="${escapeHTML(
                      world.id,
                    )}" title="恢复世界" aria-label="恢复世界">↺</button>`
                  : `<button class="icon-btn is-danger" data-world-action="archive" data-id="${escapeHTML(
                      world.id,
                    )}" title="归档" aria-label="归档">⚑</button>`
              }
            </div>
          </div>
          <h3>${escapeHTML(world.name)}</h3>
          <div class="table-subtitle">${escapeHTML(world.slug)}</div>
          <p class="world-description">${escapeHTML(world.description || "尚未填写世界简介。")}</p>
          <div class="world-stats">
            <div class="world-stat">
              <strong>${escapeHTML(
                `${world.player_limits?.recommended_min || 1}—${
                  world.player_limits?.recommended_max || 4
                }`,
              )}</strong>
              <span>推荐人数</span>
            </div>
            <div class="world-stat">
              <strong>${escapeHTML(world.player_limits?.maximum || 4)}</strong>
              <span>强制上限</span>
            </div>
            <div class="world-stat">
              <strong>${world.choice_mode === "strict_abcd" ? "A—D" : "自由"}</strong>
              <span>行动模式</span>
            </div>
          </div>
          <div class="world-actions">
            <button class="action-button" data-world-action="edit" data-id="${escapeHTML(
              world.id,
            )}">编辑设定</button>
            <button class="action-button" data-world-action="characters" data-id="${escapeHTML(
              world.id,
            )}">角色管理</button>
            <button class="action-button" data-world-action="card-template" data-id="${escapeHTML(
              world.id,
            )}">角色卡模板</button>
            <button class="action-button" data-world-action="preflight" data-id="${escapeHTML(
              world.id,
            )}">兼容性体检</button>
            ${Number(world.world_schema_version || 0) >= 5 ? `<button class="action-button" data-world-action="simulate" data-id="${escapeHTML(world.id)}">规则模拟</button>` : ""}
          </div>
        </article>
      `,
    )
    .join("");
}

function sessionActions(session) {
  if (session.state === "finished" || session.readonly) {
    return [
      ["clone", "克隆续作"],
      ["detail", "查看归档"],
    ];
  }
  const actions = [];
  if (session.state === "running") {
    actions.push(["pause", "暂停"]);
  } else if (session.state === "preparing") {
    actions.push(["perform", session.turn_no ? "继续故事" : "正式开演"]);
  } else if (session.state === "paused" || session.state === "maintenance") {
    actions.push(["resume", "恢复准备"]);
  } else {
    actions.push(["start", "进入准备"]);
  }
  if (session.state !== "closed") actions.push(["close", "关闭"]);
  if (session.state !== "closed") {
    actions.push(["finish", "完结"]);
    actions.push(["abort", "强制终止"]);
  }
  actions.push(["clone", "克隆分支"]);
  actions.push(["detail", "详情"]);
  return actions;
}

function sessionCardMarkup(session) {
  const progress = session.progress || {};
  const progressValue =
    session.progress_percent === null ||
    session.progress_percent === undefined
      ? null
      : Number(session.progress_percent);
  const waitingLabel =
    {
      vote: "等待集体投票",
      choice: "等待个人选择",
      preparation: "等待建卡与准备",
      admin: "等待管理员处理",
    }[session.waiting_for] || "暂无等待流程";
  const recovery = session.recovery || {};
  const termination =
    session.archive?.termination_type === "aborted"
      ? `强制终止：${session.archive?.reason || "未填写原因"}`
      : session.archive
        ? "故事正常完结"
        : "";
  const storageError =
    session.storage_sync_status === "error"
      ? `副本文件同步异常：${session.storage_last_error || "等待自动重试"}`
      : "";
  return `
    <article class="session-card ${session.readonly ? "is-readonly" : ""}">
      <header class="session-card-head">
        <div class="sc-head-main">
          <div class="eyebrow">PLAYTHROUGH ${escapeHTML(
            session.playthrough_no || 1,
          )} · ${escapeHTML(formatDate(session.created_at))}</div>
          <h2>${escapeHTML(session.instance_name || session.world_name)}</h2>
          <p>${escapeHTML(session.world_name)} · ${escapeHTML(
            session.instance_slug || session.world_slug,
          )}${session.selected ? " · 当前绑定" : ""}</p>
        </div>
        ${statusBadge(session.state)}
      </header>
      <div class="session-card-scene">
        <strong>${escapeHTML(session.world_state?.location || "地点未记录")}</strong>
        <span>${escapeHTML(
          session.world_state?.scene_summary || "尚无场景摘要",
        )}</span>
      </div>
      <div class="session-card-progress">
        <div>
          <span>${escapeHTML(progress.chapter || "章节未设置")}</span>
          <strong>${escapeHTML(
            progress.current_objective || "当前目标未设置",
          )}</strong>
        </div>
        ${
          progressValue === null
            ? '<span class="progress-note">未设置里程碑，不显示虚构百分比</span>'
            : `<div class="progress-meter" aria-label="剧情进度 ${progressValue}%">
                <i style="width:${Math.max(0, Math.min(100, progressValue))}%"></i>
              </div><span class="progress-note">${progressValue}% · ${escapeHTML(
                progress.completed_milestones || 0,
              )}/${escapeHTML(progress.total_milestones || 0)} 里程碑</span>`
        }
      </div>
      <div class="session-card-stats">
        <span>剧情 ${escapeHTML(session.turn_no)} 回合</span>
        <span>${escapeHTML(session.player_count || 0)} 人 · ${escapeHTML(
          session.ready_count || 0,
        )} 已准备</span>
        <span>${escapeHTML(session.npc_count || 0)} NPC</span>
        <span>${escapeHTML(session.memory_count || 0)} 记忆</span>
        <span>${escapeHTML(session.snapshot_count || 0)} 快速恢复点</span>
      </div>
      <div class="session-card-waiting">
        <span>${escapeHTML(waitingLabel)}</span>
        <span>${escapeHTML(
          session.active_deadline_at
            ? `截止 ${formatDate(session.active_deadline_at)}`
            : "不限时或未启动计时",
        )}</span>
      </div>
      ${
        termination || recovery.state === "error" || storageError
          ? `<div class="session-card-alert">${escapeHTML(
              termination ||
                storageError ||
                recovery.message ||
                "等待管理员恢复",
            )}</div>`
          : ""
      }
      <footer class="session-card-actions">
        <span>更新于 ${escapeHTML(formatDate(session.updated_at))}</span>
        <div class="table-actions">
          <button class="action-button" data-session-live="${escapeHTML(
            session.id,
          )}" title="打开副本实时仪表盘">实时</button>
          ${sessionActions(session)
            .map(
              ([action, label]) => `
                <button class="action-button ${
                  ["close", "abort"].includes(action) ? "is-danger" : ""
                }" data-session-action="${action}" data-id="${escapeHTML(
                  session.id,
                )}">${label}</button>
              `,
            )
            .join("")}
        </div>
      </footer>
    </article>
  `;
}

function renderSessions() {
  const body = $("#session-card-grid");
  $("#session-result-count").textContent = app.sessionQuery
    ? `检索到 ${app.sessionTotal} 个故事副本`
    : `共 ${app.sessionTotal} 个故事副本`;
  $("#session-page-label").textContent = `${app.sessionPage} / ${app.sessionPages}`;
  $("#session-page-prev").disabled = app.sessionPage <= 1;
  $("#session-page-next").disabled = app.sessionPage >= app.sessionPages;
  if (!app.sessions.length) {
    body.innerHTML = `
      <div class="empty-state compact">
        <div class="empty-symbol">◉</div>
        <span>${app.sessionQuery ? "没有匹配的群或故事副本" : "尚未建立群会话"}</span>
      </div>
    `;
    return;
  }
  const metaByGroup = new Map(
    app.sessionGroups.map((item) => [
      `${item.platform_id}\0${item.group_id}`,
      item,
    ]),
  );
  const grouped = new Map();
  for (const session of app.sessions) {
    const key = `${session.platform_id}\0${session.group_id}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(session);
  }
  body.innerHTML = [...grouped.entries()]
    .map(([key, sessions]) => {
      const meta = metaByGroup.get(key) || {};
      const sample = sessions[0];
      const remark = meta.remark || sample.group_remark || "";
      const displayName = remark || `群 ${sample.group_id}`;
      return `
        <section class="session-group-card">
          <header class="session-group-head">
            <div class="sg-head-main">
              <div class="eyebrow">${escapeHTML(sample.platform_id)} · GROUP</div>
              <h2>${escapeHTML(displayName)}</h2>
              <p>群 ID：${escapeHTML(sample.group_id)} · 共 ${escapeHTML(
                meta.story_count || sessions.length,
              )} 个故事副本${
                Number(meta.running_count || 0)
                  ? ` · ${escapeHTML(meta.running_count)} 个运行中`
                  : ""
              }</p>
            </div>
            <div class="table-actions">
              <button class="action-button" data-group-action="token-quota"
                data-platform-id="${escapeHTML(sample.platform_id)}"
                data-group-id="${escapeHTML(sample.group_id)}"
                data-group-name="${escapeHTML(displayName)}">群 Token 限额</button>
              <button class="action-button" data-group-action="remark"
                data-platform-id="${escapeHTML(sample.platform_id)}"
                data-group-id="${escapeHTML(sample.group_id)}"
                data-group-remark="${escapeHTML(remark)}"
                data-group-revision="${escapeHTML(
                  meta.revision || sample.group_revision || 1,
                )}">编辑群备注</button>
            </div>
          </header>
          <div class="session-card-grid">${sessions
            .map(sessionCardMarkup)
            .join("")}</div>
        </section>
      `;
    })
    .join("");
}

function renderSessionSelects() {
  const options = [
    '<option value="">选择会话</option>',
    ...app.sessionOptions.map(
      (session) =>
        `<option value="${escapeHTML(session.id)}">${escapeHTML(
          session.group_remark || `群 ${session.group_id}`,
        )} · ${escapeHTML(
          session.instance_name || session.world_name,
        )}</option>`,
    ),
  ].join("");
  const auditOptions = [
    '<option value="">全部会话</option>',
    ...app.sessionOptions.map(
      (session) =>
        `<option value="${escapeHTML(session.id)}">${escapeHTML(
          session.group_remark || `群 ${session.group_id}`,
        )} · ${escapeHTML(
          session.instance_name || session.world_name,
        )}</option>`,
    ),
  ].join("");
  const memoryValue = $("#memory-session-select").value;
  const auditValue = $("#audit-session-select").value;
  $("#memory-session-select").innerHTML = options;
  $("#audit-session-select").innerHTML = auditOptions;
  if (app.sessionOptions.some((item) => item.id === memoryValue)) {
    $("#memory-session-select").value = memoryValue;
  } else if (app.sessionOptions[0]) {
    $("#memory-session-select").value = app.sessionOptions[0].id;
  }
  $("#audit-session-select").value = app.sessionOptions.some(
    (item) => item.id === auditValue,
  )
    ? auditValue
    : "";
}

// A14（审计 #5）：角色卡修订请求 / 审核统一封装，避免四处重复 apiPost。
async function requestCardRevision({
  sessionId,
  participantRef,
  profilePatch,
  statsPatch = {},
  note = "",
}) {
  return bridge.apiPost("sessions/card-revisions", {
    action: "request",
    session_id: sessionId,
    participant_ref: participantRef,
    profile_patch: profilePatch,
    stats_patch: statsPatch,
    note,
  });
}
async function reviewCardRevision(requestId, approved, sessionId) {
  await bridge.apiPost("sessions/card-revisions", {
    action: approved ? "approve" : "reject",
    request_id: requestId,
    note: `WebUI ${approved ? "审核通过" : "审核拒绝"}`,
  });
  toast(approved ? "角色卡新版本已启用" : "角色卡修改已拒绝", "success");
  await openSessionDetail(sessionId);
}

function openEditor({ title, kicker = "EDITOR", body, saveLabel = "保存", onSave }) {
  $("#editor-modal-title").textContent = title;
  $("#editor-modal-kicker").textContent = kicker;
  $("#editor-modal-body").innerHTML = body;
  $("#editor-save-button").textContent = saveLabel;
  $("#editor-modal-foot").classList.toggle("is-hidden", !onSave);
  app.editorSave = onSave || null;
  if (!$("#editor-modal").open) {
    $("#editor-modal").showModal();
  }
}

// A9：统一的单行/多行文本输入弹窗，替代被 iframe 拦截的 window.prompt。
// resolve(null) 表示用户取消；resolve("") 表示必填未通过（此时弹窗不关闭）。
function promptForText(arg = {}) {
  // A18: 兼容字符串与对象两种调用；字符串时标题与说明共用同一文本。
  const config =
    typeof arg === "string" ? { title: arg, label: arg } : arg;
  const {
    title,
    kicker = "INPUT",
    label,
    placeholder = "",
    defaultValue = "",
    required = false,
    multiline = false,
    saveLabel = "确定",
  } = config;
  return new Promise((resolve) => {
    let settled = false;
    const onClose = () => {
      if (!settled) {
        settled = true;
        resolve(null);
      }
    };
    $("#editor-modal").addEventListener("close", onClose, { once: true });
    openEditor({
      title,
      kicker,
      body: `
        <div class="field">
          <label for="prompt-text">${escapeHTML(label)}</label>
          ${
            multiline
              ? `<textarea id="prompt-text" class="code-field" rows="6"
                  maxlength="4000" placeholder="${escapeHTML(
                    placeholder,
                  )}">${escapeHTML(defaultValue)}</textarea>`
              : `<input id="prompt-text" maxlength="2000"
                  placeholder="${escapeHTML(
                    placeholder,
                  )}" value="${escapeHTML(defaultValue)}" />`
          }
        </div>`,
      saveLabel,
      onSave: async () => {
        const value = ($("#prompt-text")?.value ?? "").trim();
        if (required && !value) throw new Error("该项不能为空");
        settled = true;
        resolve(value);
      },
    });
  });
}

function openWorldEditor(world = null) {
  const item =
    world ||
    {
      slug: "",
      name: "",
      description: "",
      system_prompt: "",
      opening_scene: "",
      world_schema_version: 5,
      minimum_plugin_version: "0.12.0",
      protocol: { core_version: 5, features: {} },
      required_features: [],
      rules: {
        resolution: "d20",
        default_difficulty: 12,
        difficulty_min: 5,
        difficulty_max: 25,
        allow_player_result_claims: false,
        strict_choices: true,
        player_limits: {
          recommended_min: 2,
          recommended_max: 4,
          minimum_start: 2,
          maximum: 4,
        },
      },
      initial_state: {
        location: "",
        time: "",
        scene_summary: "",
        facts: [],
        inventory: {},
        relationships: {},
      },
    };
  const rulesForEditor = { ...(item.rules || {}) };
  const protocolEnvelope = {
    protocol: item.protocol || { core_version: Number(item.world_schema_version || 5), features: {} },
    required_features: item.required_features || [],
    id_aliases: item.id_aliases || {},
    numeric_policies: item.numeric_policies || {},
  };
  const characterCardTemplate =
    rulesForEditor.character_card ||
    item.card_template ||
    structuredClone(DEFAULT_CHARACTER_CARD_TEMPLATE);
  delete rulesForEditor.character_card;
  const chatExperience = {
    enabled: false,
    character_creation: { primary: "private_code", fallbacks: ["webui_token"] },
    multiplayer: { spotlight: "round_robin", group_decisions: "vote", absent_player: "standby" },
    safety: { enabled: true, anonymous_pause: false, consent_reminder: "", lines: [], veils: [] },
    continuity: { recap_every_turns: 0, checkpoint_every_turns: 0, unresolved_threads_limit: 8, preserve_npc_intent: true },
    delivery: { proactive_fallback: "next_event", mention_style: "name", max_text_length: 3500 },
    dm: { allow_narrative_override: true, allow_secret_whispers: true, allow_manual_checks: true, allow_state_intervention: true },
    ...(rulesForEditor.chat_experience || item.chat_experience || {}),
  };
  chatExperience.character_creation = { primary: "private_code", fallbacks: ["webui_token"], ...(chatExperience.character_creation || {}) };
  chatExperience.multiplayer = { spotlight: "round_robin", group_decisions: "vote", absent_player: "standby", ...(chatExperience.multiplayer || {}) };
  chatExperience.safety = { enabled: true, anonymous_pause: false, consent_reminder: "", lines: [], veils: [], ...(chatExperience.safety || {}) };
  chatExperience.continuity = { recap_every_turns: 0, checkpoint_every_turns: 0, unresolved_threads_limit: 8, preserve_npc_intent: true, ...(chatExperience.continuity || {}) };
  chatExperience.delivery = { proactive_fallback: "next_event", mention_style: "name", max_text_length: 3500, ...(chatExperience.delivery || {}) };
  chatExperience.dm = { allow_narrative_override: true, allow_secret_whispers: true, allow_manual_checks: true, allow_state_intervention: true, ...(chatExperience.dm || {}) };
  delete rulesForEditor.chat_experience;
  openEditor({
    title: world ? `编辑「${world.name}」` : "新建世界包",
    kicker: "WORLD DEFINITION",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="world-name">世界名称</label>
          <input id="world-name" value="${escapeHTML(item.name)}" maxlength="400" />
        </div>
        <div class="field">
          <label for="world-slug">唯一标识</label>
          <input id="world-slug" value="${escapeHTML(item.slug)}" maxlength="64"
            placeholder="lowercase-slug" />
        </div>
        <div class="field">
          <label for="world-schema-version">世界核心协议</label>
          <select id="world-schema-version">
            ${[2, 3, 4, 5].map((value) => `<option value="${value}" ${Number(item.world_schema_version || 5) === value ? "selected" : ""}>v${value}${value === 5 ? " · 模块化" : " · 兼容模式"}</option>`).join("")}
          </select>
        </div>
        <div class="field field-span-2">
          <label for="world-protocol-envelope">协议功能与迁移映射 JSON</label>
          <textarea id="world-protocol-envelope" class="code-field" rows="10">${escapeHTML(prettyJSON(protocolEnvelope))}</textarea>
          <small>v5 只加载 protocol.features 与 required_features 明确启用的模块；未启用模块不会成为必填项。能力、资源、对象、持续效果、裁定和交互规则可继续在下方规则 JSON 中编辑。</small>
        </div>
        <div class="field field-span-2">
          <label for="world-description">简介</label>
          <textarea id="world-description" rows="3">${escapeHTML(
            item.description,
          )}</textarea>
        </div>
        <div class="field">
          <label for="world-recommended-min">推荐最少人数</label>
          <input id="world-recommended-min" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.recommended_min ||
                item.rules?.player_limits?.recommended_min ||
                2,
            )}" />
        </div>
        <div class="field">
          <label for="world-recommended-max">推荐最多人数</label>
          <input id="world-recommended-max" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.recommended_max ||
                item.rules?.player_limits?.recommended_max ||
                4,
            )}" />
        </div>
        <div class="field">
          <label for="world-minimum-start">最低开演人数</label>
          <input id="world-minimum-start" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.minimum_start ||
                item.rules?.player_limits?.minimum_start ||
                2,
            )}" />
        </div>
        <div class="field">
          <label for="world-maximum">强制人数上限</label>
          <input id="world-maximum" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.maximum || item.rules?.player_limits?.maximum || 4,
            )}" />
        </div>
        <label class="switch-field field-span-2">
          <input id="world-strict-choices" type="checkbox" ${
            item.rules?.strict_choices !== false ? "checked" : ""
          } />
          <span><strong>严格 A/B/C/D 回合</strong>
            <small>关键集体决定自动转为多数表决</small></span>
        </label>
        <div class="field">
          <label for="world-stats-mode">角色数值模式</label>
          <select id="world-stats-mode">
            ${[["none", "无数值"], ["manual", "手动分配"], ["preset", "职业预设"], ["preset_stack", "多预设自动结算"]].map(([value, label]) => `<option value="${value}" ${String(characterCardTemplate.stats?.mode || "manual") === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label for="world-resolution-mode">裁定模式</label>
          <select id="world-resolution-mode">
            ${[["none", "不裁定"], ["narrative", "叙事裁定"], ["dice_only", "纯骰检定"], ["attribute", "属性检定"]].map(([value, label]) => `<option value="${value}" ${String(item.rules?.resolution?.mode || (typeof item.rules?.resolution === "string" ? "attribute" : "none")) === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="field field-span-2">
          <label for="world-dice-system">骰制</label>
          <select id="world-dice-system"><option value="d20" ${String(item.rules?.resolution?.dice_system || "d20") === "d20" ? "selected" : ""}>D20</option><option value="none" ${String(item.rules?.resolution?.dice_system || "") === "none" ? "selected" : ""}>无骰制</option></select>
          <small>可视化字段会写回纯 JSON；复杂属性、职业和事件仍可在下方源码区精确调整。</small>
        </div>
        <details class="settings-disclosure field-span-2" ${chatExperience.enabled ? "open" : ""}>
          <summary>
            <span><strong>多人群聊体验</strong><small>可选模块 · 旧世界包默认不启用</small></span>
            <span class="status-badge ${chatExperience.enabled ? "status-running" : "status-closed"}">${chatExperience.enabled ? "已启用" : "未启用"}</span>
          </summary>
          <div class="form-grid disclosure-body">
            <label class="switch-field field-span-2">
              <input id="world-chat-enabled" type="checkbox" ${chatExperience.enabled ? "checked" : ""} />
              <span><strong>启用群聊体验策略</strong><small>启用后才写入 chat_experience@1.0；关闭时所有策略均为无操作。</small></span>
            </label>
            <div class="field"><label for="world-chat-card-mode">主要建卡方式</label>
              <select id="world-chat-card-mode">${[["private_code","私聊绑定码"],["webui_token","WebUI 管理员代建"]].map(([value,label]) => `<option value="${value}" ${chatExperience.character_creation.primary === value ? "selected" : ""}>${label}</option>`).join("")}</select>
              <small>优先使用跨平台私聊绑定码；平台无法私聊时由有副本权限的管理员在 WebUI 角色页代建或维护。</small></div>
            <div class="field"><label for="world-chat-spotlight">多人聚光方式</label>
              <select id="world-chat-spotlight">${[["round_robin","严格轮流"],["soft_round_robin","柔性轮流"],["free","自由行动"]].map(([value,label]) => `<option value="${value}" ${chatExperience.multiplayer.spotlight === value ? "selected" : ""}>${label}</option>`).join("")}</select>
              <small>用于约束叙事节奏；当前插件的权威回合表仍负责最终行动权。</small></div>
            <div class="field"><label for="world-chat-decisions">集体决策方式</label>
              <select id="world-chat-decisions">${[["vote","多数投票"],["consensus","共识优先"],["host","主持裁定"]].map(([value,label]) => `<option value="${value}" ${chatExperience.multiplayer.group_decisions === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
            <div class="field"><label for="world-chat-absent">缺席玩家处理</label>
              <select id="world-chat-absent">${[["standby","转为候补"],["delegate","优先托管"],["skip","跳过回合"]].map(([value,label]) => `<option value="${value}" ${chatExperience.multiplayer.absent_player === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
            <div class="field"><label for="world-chat-recap">自动回顾间隔（回合）</label><input id="world-chat-recap" type="number" min="0" max="50" value="${escapeHTML(chatExperience.continuity.recap_every_turns)}" /><small>0 表示不由世界包要求周期回顾。</small></div>
            <div class="field"><label for="world-chat-checkpoint">检查点间隔（回合）</label><input id="world-chat-checkpoint" type="number" min="0" max="50" value="${escapeHTML(chatExperience.continuity.checkpoint_every_turns)}" /><small>0 使用全局自动快照策略。</small></div>
            <div class="field"><label for="world-chat-thread-limit">未决剧情线索上限</label><input id="world-chat-thread-limit" type="number" min="0" max="30" value="${escapeHTML(chatExperience.continuity.unresolved_threads_limit)}" /></div>
            <div class="field"><label for="world-chat-delivery">主动通知失败</label><select id="world-chat-delivery">${[["next_event","下次消息补发"],["webui_only","仅在控制台保留"],["discard","丢弃通知"]].map(([value,label]) => `<option value="${value}" ${chatExperience.delivery.proactive_fallback === value ? "selected" : ""}>${label}</option>`).join("")}</select><small>推荐“下次消息补发”，可跨重启恢复。</small></div>
            <div class="field field-span-2"><label for="world-chat-consent">安全提示</label><input id="world-chat-consent" maxlength="500" value="${escapeHTML(chatExperience.safety.consent_reminder || "")}" placeholder="例如：任何玩家均可安全暂停，无需解释原因。" /></div>
            <div class="field"><label for="world-chat-lines">不可出现内容（每行一项）</label><textarea id="world-chat-lines" rows="4" placeholder="硬边界；叙事不得出现">${escapeHTML((chatExperience.safety.lines || []).join("\n"))}</textarea></div>
            <div class="field"><label for="world-chat-veils">淡出处理内容（每行一项）</label><textarea id="world-chat-veils" rows="4" placeholder="可以存在，但不作细节描写">${escapeHTML((chatExperience.safety.veils || []).join("\n"))}</textarea></div>
            <label class="switch-field"><input id="world-chat-npc-intent" type="checkbox" ${chatExperience.continuity.preserve_npc_intent !== false ? "checked" : ""} /><span><strong>保持 NPC 意图连续</strong><small>避免跨回合突然改变态度或目标。</small></span></label>
            <label class="switch-field"><input id="world-chat-dm-whisper" type="checkbox" ${chatExperience.dm.allow_secret_whispers !== false ? "checked" : ""} /><span><strong>允许 DM 私密投递</strong><small>目标未绑定私聊时进入待投递队列。</small></span></label>
            <label class="switch-field"><input id="world-chat-dm-narrative" type="checkbox" ${chatExperience.dm.allow_narrative_override !== false ? "checked" : ""} /><span><strong>允许 DM 改写叙事</strong><small>控制人工 DM 控制台的插入/覆盖剧情能力。</small></span></label>
            <label class="switch-field"><input id="world-chat-dm-check" type="checkbox" ${chatExperience.dm.allow_manual_checks !== false ? "checked" : ""} /><span><strong>允许 DM 手动检定</strong><small>允许主持人记录权威检定结果并同步群聊。</small></span></label>
            <label class="switch-field"><input id="world-chat-dm-state" type="checkbox" ${chatExperience.dm.allow_state_intervention !== false ? "checked" : ""} /><span><strong>允许 DM 干预状态</strong><small>包括行动权、锁定、选项、关系与经济调整；操作均写审计。</small></span></label>
          </div>
        </details>
        <div class="field field-span-2">
          <label for="world-system-prompt">核心世界设定</label>
          <textarea id="world-system-prompt" rows="9">${escapeHTML(
            item.system_prompt,
          )}</textarea>
          <small>这里只写稳定、不可随剧情轻易改变的世界规律。</small>
        </div>
        <div class="field field-span-2">
          <label for="world-opening">开场场景</label>
          <textarea id="world-opening" rows="5">${escapeHTML(
            item.opening_scene,
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="world-rules">裁定规则 JSON</label>
          <textarea id="world-rules" class="code-field">${escapeHTML(
            prettyJSON(rulesForEditor),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <div class="field-label-row">
            <label for="world-character-card">玩家角色卡模板 JSON</label>
            <div class="table-actions">
              <button type="button" class="action-button" id="preview-character-card">
                预览建卡流程
              </button>
              <button type="button" class="action-button" id="restore-character-card">
                恢复默认模板
              </button>
            </div>
          </div>
          <textarea id="world-character-card" class="code-field" rows="18">${escapeHTML(
            prettyJSON(characterCardTemplate),
          )}</textarea>
          <small>独立校验模板版本、重复字段、name/code 必需字段、属性预算与范围；常驻 NPC 不在这里管理。</small>
          <div class="template-preview" id="world-character-card-preview" hidden></div>
        </div>
        <div class="field field-span-2">
          <div class="field-label-row">
            <label for="world-elemental">元素反应配置 JSON</label>
            <div class="table-actions">
              <button type="button" class="action-button" id="preview-elemental">预览元素表</button>
            </div>
          </div>
          <textarea id="world-elemental" class="code-field" rows="10">${escapeHTML(
            prettyJSON(item.elemental || {}),
          )}</textarea>
          <small>声明元素、目标亲和（-2..2，负=克制）与双元素反应；可选 resolver 走 element_resolver 扩展点。</small>
          <div class="template-preview" id="world-elemental-preview" hidden></div>
          <div class="elemental-dryrun" id="elemental-dryrun" hidden>
            <div class="form-grid" style="margin-top:12px">
              <div class="field"><label for="elemental-source">施放元素</label>
                <input id="elemental-source" placeholder="例如：火" /></div>
              <div class="field"><label for="elemental-target">目标引用</label>
                <input id="elemental-target" placeholder="例如：npc:炎魔" /></div>
              <div class="field"><label for="elemental-target-element">目标当前元素（可选）</label>
                <input id="elemental-target-element" placeholder="例如：冰" /></div>
              <div class="field"><label>&nbsp;</label>
                <button class="button button-primary" id="elemental-dryrun-button" type="button">解析反应</button></div>
            </div>
            <pre class="json-preview" id="elemental-dryrun-result"></pre>
          </div>
        </div>
        <div class="field field-span-2">
          <label for="world-state">初始世界状态 JSON</label>
          <textarea id="world-state" class="code-field">${escapeHTML(
            prettyJSON(item.initial_state),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      const rules = parseJSONField("#world-rules", "裁定规则");
      const protocol = parseJSONField("#world-protocol-envelope", "协议功能与迁移映射");
      const schemaVersion = Number($("#world-schema-version").value);
      const characterCard = parseJSONField("#world-character-card", "玩家角色卡模板");
      characterCard.stats = characterCard.stats || {};
      characterCard.stats.mode = $("#world-stats-mode").value;
      if (characterCard.stats.mode === "none") {
        characterCard.stats.attributes = [];
        characterCard.stats.modifier_table = {};
        characterCard.stats.budget = 0;
      }
      rules.character_card = validateCharacterCardTemplate(characterCard);
      rules.resolution = {
        ...(typeof rules.resolution === "object" ? rules.resolution : {}),
        mode: $("#world-resolution-mode").value,
        dice_system: $("#world-dice-system").value,
        unknown_attribute: "reject",
      };
      rules.strict_choices = $("#world-strict-choices").checked;
      rules.player_limits = {
        recommended_min: Number($("#world-recommended-min").value),
        recommended_max: Number($("#world-recommended-max").value),
        minimum_start: Number($("#world-minimum-start").value),
        maximum: Number($("#world-maximum").value),
      };
      const lines = (selector) => selector.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      rules.chat_experience = {
        enabled: $("#world-chat-enabled").checked,
        character_creation: {
          primary: $("#world-chat-card-mode").value,
          fallbacks: ["webui_token"],
        },
        multiplayer: {
          spotlight: $("#world-chat-spotlight").value,
          group_decisions: $("#world-chat-decisions").value,
          absent_player: $("#world-chat-absent").value,
        },
        safety: {
          enabled: true,
          anonymous_pause: false,
          consent_reminder: $("#world-chat-consent").value.trim(),
          lines: lines($("#world-chat-lines")),
          veils: lines($("#world-chat-veils")),
        },
        continuity: {
          recap_every_turns: Number($("#world-chat-recap").value || 0),
          checkpoint_every_turns: Number($("#world-chat-checkpoint").value || 0),
          unresolved_threads_limit: Number($("#world-chat-thread-limit").value || 8),
          preserve_npc_intent: $("#world-chat-npc-intent").checked,
        },
        delivery: {
          proactive_fallback: $("#world-chat-delivery").value,
          mention_style: "name",
          max_text_length: 3500,
        },
        dm: {
          allow_narrative_override: $("#world-chat-dm-narrative").checked,
          allow_secret_whispers: $("#world-chat-dm-whisper").checked,
          allow_manual_checks: $("#world-chat-dm-check").checked,
          allow_state_intervention: $("#world-chat-dm-state").checked,
        },
      };
      protocol.protocol = protocol.protocol || { core_version: 5, features: {} };
      protocol.protocol.features = protocol.protocol.features || {};
      if (rules.chat_experience.enabled) {
        protocol.protocol.features.chat_experience = "1.0";
      } else {
        delete protocol.protocol.features.chat_experience;
      }
      const payload = {
        id: item.id,
        revision: item.revision,
        name: $("#world-name").value,
        slug: $("#world-slug").value,
        description: $("#world-description").value,
        system_prompt: $("#world-system-prompt").value,
        opening_scene: $("#world-opening").value,
        rules,
        initial_state: parseJSONField("#world-state", "初始世界状态"),
        elemental: parseJSONField("#world-elemental", "元素反应配置"),
        archived: Boolean(item.archived),
        world_schema_version: schemaVersion,
        minimum_plugin_version: schemaVersion >= 5 ? "0.12.0" : (
          item.minimum_plugin_version || (characterCard.stats.mode === "preset_stack" ? "0.9.3" : "")
        ),
        protocol: schemaVersion >= 5 ? protocol.protocol : item.protocol,
        required_features: schemaVersion >= 5 ? (protocol.required_features || []) : (item.required_features || []),
        id_aliases: protocol.id_aliases || {},
        numeric_policies: protocol.numeric_policies || {},
        ...(schemaVersion < 5 ? {capabilities: {
          character_stats: characterCard.stats.mode !== "none",
          attribute_checks: rules.resolution.mode === "attribute",
          dice_resolution: ["dice_only", "attribute"].includes(rules.resolution.mode),
        }} : {}),
      };
      await bridge.apiPost("worlds/save", payload);
      toast(world ? "世界包已更新" : "世界包已创建", "success");
      await loadCore();
    },
  });
  $("#restore-character-card").addEventListener("click", () => {
    $("#world-character-card").value = prettyJSON(DEFAULT_CHARACTER_CARD_TEMPLATE);
    toast("已恢复默认角色卡模板；保存世界后生效", "success");
  });
  $("#preview-elemental").addEventListener("click", () => {
    const panel = $("#world-elemental-preview");
    const dry = $("#elemental-dryrun");
    try {
      const elemental = parseJSONField("#world-elemental", "元素反应配置");
      panel.innerHTML = renderElementalTablePreview(elemental);
      panel.hidden = false;
      dry.hidden = false;
      toast("元素表结构解析通过", "success");
    } catch (error) {
      panel.innerHTML = `<p class="template-preview-error">${escapeHTML(
        error?.message || String(error),
      )}</p>`;
      panel.hidden = false;
      showError(error);
    }
  });
  $("#elemental-dryrun-button").addEventListener("click", async () => {
    const out = $("#elemental-dryrun-result");
    try {
      const elemental = parseJSONField("#world-elemental", "元素反应配置");
      const source = $("#elemental-source").value.trim();
      const target = $("#elemental-target").value.trim();
      if (!source || !target) throw new Error("请填写施放元素与目标引用");
      const resp = await bridge.apiPost("worlds/element-reaction", {
        world: { ...item, elemental },
        source,
        target,
        target_element: $("#elemental-target-element").value.trim() || undefined,
      });
      const reaction = resp.reaction;
      out.textContent = reaction
        ? `命中：${reaction.source} → ${reaction.target}${
            reaction.target_element ? `（${reaction.target_element}）` : ""
          }\n亲和：${reaction.affinity}\n反应：${
            reaction.reaction ? reaction.reaction.result : "无（仅亲和加成）"
          }\n效果：${JSON.stringify(reaction.effects, null, 2)}`
        : "无命中：既无亲和加成也无双元素反应。";
    } catch (error) {
      out.textContent = `解析失败：${error?.message || error}`;
      showError(error);
    }
  });
  $("#preview-character-card").addEventListener("click", () => {
    // 预览必须自带异常处理：模板缺少 fields / JSON 非法时会抛错，
    // 之前既没有 try/catch 也没有页面内反馈，表现为“点了没反应”。
    const panel = $("#world-character-card-preview");
    try {
      const template = validateCharacterCardTemplate(
        parseJSONField("#world-character-card", "玩家角色卡模板"),
      );
      // 不再使用 window.alert：控制台以 iframe 形式嵌在 AstrBot 面板中，
      // 浏览器会静默拦截跨源 iframe 里的模态框，导致点击毫无反馈。
      panel.innerHTML = renderCharacterCardTemplatePreview(template);
      panel.hidden = false;
      toast(
        `模板 v${template.version} 校验通过 · ${template.fields.length} 个字段`,
        "success",
      );
    } catch (error) {
      panel.innerHTML = `<p class="template-preview-error">${escapeHTML(
        error?.message || String(error),
      )}</p>`;
      panel.hidden = false;
      showError(error);
    }
  });
}

function renderElementalTablePreview(elemental) {
  const parsed = elemental && typeof elemental === "object" ? elemental : {};
  const elements = (parsed.elements || [])
    .map((item) => `<span class="ws-chip">${escapeHTML(item)}</span>`)
    .join("");
  const affinities = Object.entries(parsed.affinities || {})
    .map(
      ([ref, table]) =>
        `<div class="ws-tile"><span>${escapeHTML(ref)}</span><strong>${Object.entries(
          table,
        )
          .map(([element, value]) => `${escapeHTML(element)} ${Number(value) > 0 ? "+" : ""}${value}`)
          .join(" · ")}</strong></div>`,
    )
    .join("");
  const reactions = (parsed.reactions || [])
    .map(
      (item) =>
        `<div class="option-card"><span class="option-key">${escapeHTML(
          item.a || "?",
        )}</span><span class="option-text">${escapeHTML(
          `${item.a} + ${item.b} → ${item.result}`,
        )}</span><span class="option-tag">${escapeHTML(
          item.severity || "normal",
        )}</span></div>`,
    )
    .join("");
  return `
    <div class="ws-visual">
      ${
        elements
          ? `<section class="pcc-section"><div class="pcc-section-head"><h4>⚡ 元素</h4><small>${parsed.elements.length} 个</small></div><div class="ws-chip-list">${elements}</div></section>`
          : ""
      }
      ${
        affinities
          ? `<section class="pcc-section"><div class="pcc-section-head"><h4>🛡 亲和 / 抗性</h4></div><div class="ws-tile-grid">${affinities}</div></section>`
          : ""
      }
      ${
        reactions
          ? `<section class="pcc-section"><div class="pcc-section-head"><h4>⚗ 双元素反应</h4></div><div class="option-list">${reactions}</div></section>`
          : ""
      }
      ${
        !elements && !affinities && !reactions
          ? '<p class="field-hint">尚未声明元素反应配置。</p>'
          : ""
      }
    </div>`;
}

function renderCharacterCardTemplatePreview(template) {
  // A9：预览建卡流程升级为「角色卡成品预览」——属性进度条、建卡步骤网格、
  // 模式/预算/审核策略徽标，整体贴合玻璃卡片设计体系。
  const stats = template.stats || {};
  const attributes = Array.isArray(stats.attributes) ? stats.attributes : [];
  const modeLabel =
    {
      none: "无数值",
      manual: "手动分配",
      preset: "预设数值",
      preset_stack: "预设栈",
    }[stats.mode] || stats.mode || "—";
  const statBars = attributes
    .map((item) => {
      const min = Number(item.minimum) || 0;
      const max = Number(item.maximum) || 1;
      const def = item.default === undefined || item.default === null
        ? min
        : Number(item.default);
      const width = Math.max(
        4,
        Math.min(100, ((def - min) / (max - min || 1)) * 100),
      );
      return `
        <div class="pcc-stat">
          <div class="pcc-stat-label">
            <span>${escapeHTML(item.label || item.key)}</span>
            <b>${escapeHTML(def)}</b>
          </div>
          <div class="pcc-bar"><i style="width:${width}%"></i></div>
          <small>${escapeHTML(min)} — ${escapeHTML(max)}</small>
        </div>`;
    })
    .join("");
  const fields = template.fields
    .map(
      (field, index) => `
        <div class="pcc-field">
          <strong>${escapeHTML(field.label || field.key)}</strong>
          <small>${escapeHTML(field.key)} · ${
            field.required ? "必填" : "选填"
          }${field.private ? " · 私密" : ""} · 最多 ${escapeHTML(
            field.max_chars || "—",
          )} 字</small>
        </div>`,
    )
    .join("");
  const modifiers = Object.entries(stats.modifier_table || {})
    .map(
      ([key, value]) =>
        `<span>${escapeHTML(key)} → ${escapeHTML(
          Number(value) >= 0 ? `+${value}` : value,
        )}</span>`,
    )
    .join("");
  return `
    <div class="preview-character-card">
      <div class="pcc-head">
        <div>
          <strong>角色卡模板 v${escapeHTML(template.version)}</strong>
          <div class="pcc-badges" style="margin-top:6px">
            <span class="status-badge status-running">${escapeHTML(
              modeLabel,
            )}</span>
            <span class="status-badge">${escapeHTML(
              template.fields.length,
            )} 个字段</span>
            <span class="status-badge">属性预算 ${escapeHTML(
              stats.budget ?? "—",
            )}</span>
          </div>
        </div>
        <span class="field-hint">${
          template.auto_approve ? "提交后自动通过" : "提交后进入 GM 审核"
        }</span>
      </div>
      ${
        attributes.length
          ? `
      <section class="pcc-section">
        <div class="pcc-section-head">
          <h4>ATTRIBUTES · 属性与默认值</h4>
          <small>玩家建卡时以此为基准分配</small>
        </div>
        <div class="pcc-stats">${statBars}</div>
        ${
          modifiers
            ? `<div class="pcc-modifiers">${modifiers}</div>`
            : ""
        }
      </section>`
          : ""
      }
      <section class="pcc-section">
        <div class="pcc-section-head">
          <h4>FIELDS · 建卡步骤</h4>
          <small>按顺序引导玩家填写</small>
        </div>
        <div class="pcc-fields">${fields}</div>
      </section>
    </div>
  `;
}

function downloadJSON(filename, payload) {
  const blob = new Blob([`${prettyJSON(payload)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function openCharacterCardTemplateManager(world) {
  const template =
    world.rules?.character_card ||
    world.card_template ||
    structuredClone(DEFAULT_CHARACTER_CARD_TEMPLATE);
  openEditor({
    title: `${world.name} · 角色卡模板`,
    kicker: "CHARACTER CARD TEMPLATE",
    body: `
      <div class="template-manager">
        <div class="section-toolbar template-toolbar">
          <p>导入只读取本地 JSON；通过结构校验并确认保存后才会影响以后新建的角色卡。</p>
          <div class="toolbar-actions">
            <input id="character-template-file" type="file"
              accept=".json,application/json" class="sr-only" />
            <button type="button" class="action-button"
              id="character-template-import">导入 JSON</button>
            <button type="button" class="action-button"
              id="character-template-export">导出 JSON</button>
            <button type="button" class="action-button"
              id="character-template-default">恢复默认</button>
            <button type="button" class="action-button"
              id="character-template-preview-button">预览表单</button>
          </div>
        </div>
        <div class="field">
          <label for="character-template-json">玩家角色卡模板 JSON</label>
          <textarea id="character-template-json" class="code-field"
            rows="20">${escapeHTML(prettyJSON(template))}</textarea>
          <small id="character-template-status">尚未保存；导入和编辑不会立即生效。</small>
        </div>
        <div class="template-preview" id="character-template-preview"></div>
      </div>
    `,
    saveLabel: "验证并保存模板",
    onSave: async () => {
      const nextTemplate = validateCharacterCardTemplate(
        parseJSONField("#character-template-json", "玩家角色卡模板"),
      );
      const rules = {
        ...(world.rules || {}),
        character_card: nextTemplate,
      };
      await bridge.apiPost("worlds/save", {
        id: world.id,
        revision: world.revision,
        slug: world.slug,
        name: world.name,
        description: world.description,
        system_prompt: world.system_prompt,
        opening_scene: world.opening_scene,
        rules,
        initial_state: world.initial_state,
        world_schema_version: Number(
          world.world_schema_version || world.rules?.world_schema_version || 3,
        ),
        capabilities:
          world.capabilities || world.rules?.capabilities || {},
        minimum_plugin_version:
          world.minimum_plugin_version ||
          (nextTemplate.stats?.mode === "preset_stack" ? "0.9.3" : ""),
      });
      toast("角色卡模板已校验并保存", "success");
      await loadCore();
    },
  });

  const updatePreview = () => {
    const nextTemplate = validateCharacterCardTemplate(
      parseJSONField("#character-template-json", "玩家角色卡模板"),
    );
    $("#character-template-preview").innerHTML =
      renderCharacterCardTemplatePreview(nextTemplate);
    $("#character-template-status").textContent =
      `模板 v${nextTemplate.version} 校验通过；确认保存后生效。`;
    return nextTemplate;
  };
  $("#character-template-import").addEventListener("click", () => {
    $("#character-template-file").click();
  });
  $("#character-template-file").addEventListener("change", async (event) => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) {
        throw new Error("角色卡模板文件不能超过 1 MiB");
      }
      const parsed = JSON.parse(await file.text());
      const imported =
        parsed?.character_card && typeof parsed.character_card === "object"
          ? parsed.character_card
          : parsed;
      validateCharacterCardTemplate(imported);
      $("#character-template-json").value = prettyJSON(imported);
      updatePreview();
      toast(`已导入 ${file.name}；尚未保存`, "success");
    } catch (error) {
      showError(
        new Error(`模板导入失败：${error?.message || String(error)}`),
      );
    } finally {
      event.currentTarget.value = "";
    }
  });
  $("#character-template-export").addEventListener("click", () => {
    const current = updatePreview();
    downloadJSON(
      `${world.slug}-character-card-template-v${current.version}.json`,
      current,
    );
    toast("角色卡模板已导出", "success");
  });
  $("#character-template-default").addEventListener("click", () => {
    $("#character-template-json").value = prettyJSON(
      DEFAULT_CHARACTER_CARD_TEMPLATE,
    );
    updatePreview();
    toast("已载入默认模板；尚未保存", "success");
  });
  $("#character-template-preview-button").addEventListener(
    "click",
    updatePreview,
  );
  updatePreview();
}

async function openCharacterManager(worldId) {
  const world = app.worlds.find((item) => item.id === worldId);
  const result = await bridge.apiGet("characters", { world_id: worldId });
  const characters = result.items || [];
  openEditor({
    title: `${world?.name || "世界"} · 角色管理`,
    kicker: "RESIDENT CHARACTERS",
    body: `
      <div class="section-toolbar">
        <p>角色拥有独立知识边界、资料与叙事方向。</p>
        <button type="button" class="button button-primary" data-character-action="new"
          data-world-id="${escapeHTML(worldId)}">＋ 新建角色</button>
      </div>
      <div class="session-stack">
        ${
          characters.length
            ? characters
                .map(
                  (character) => `
                    <div class="session-row">
                      <div>
                        <div class="session-name">${escapeHTML(character.name)}</div>
                        <div class="session-meta">${escapeHTML(character.slug)} · ${escapeHTML(
                          character.role,
                        )}</div>
                      </div>
                      <div class="session-location">${
                        character.enabled ? "已启用" : "已停用"
                      }</div>
                      <div class="table-actions">
                        <button type="button" class="action-button"
                          data-character-action="edit" data-id="${escapeHTML(
                            character.id,
                          )}" data-world-id="${escapeHTML(worldId)}">编辑</button>
                        <button type="button" class="action-button is-danger"
                          data-character-action="delete" data-id="${escapeHTML(
                            character.id,
                          )}" data-world-id="${escapeHTML(worldId)}">删除</button>
                      </div>
                    </div>
                  `,
                )
                .join("")
            : `
              <div class="empty-state compact">
                <div class="empty-symbol">♙</div>
                <span>这个世界还没有常驻角色</span>
              </div>
            `
        }
      </div>
    `,
  });
  app.currentCharacters = characters;
}

function openCharacterEditor(worldId, character = null) {
  const item =
    character ||
    {
      world_id: worldId,
      name: "",
      slug: "",
      role: "npc",
      profile: {
        identity: "",
        appearance: "",
        personality: "",
        knowledge_boundary: "",
      },
      prompt: "",
      enabled: true,
      sort_order: 0,
    };
  openEditor({
    title: character ? `编辑角色「${character.name}」` : "新建常驻角色",
    kicker: "CHARACTER CARD",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="character-name">角色名称</label>
          <input id="character-name" value="${escapeHTML(item.name)}" />
        </div>
        <div class="field">
          <label for="character-slug">唯一标识</label>
          <input id="character-slug" value="${escapeHTML(item.slug)}" />
        </div>
        <div class="field">
          <label for="character-role">角色类型</label>
          <select id="character-role">
            ${["npc", "narrator", "faction", "creature"]
              .map(
                (role) =>
                  `<option value="${role}" ${
                    item.role === role ? "selected" : ""
                  }>${role}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="character-order">排序</label>
          <input id="character-order" type="number" value="${escapeHTML(
            item.sort_order,
          )}" />
        </div>
        <label class="switch-field field-span-2">
          <input id="character-enabled" type="checkbox" ${
            item.enabled ? "checked" : ""
          } />
          <span><strong>参与当前世界演算</strong><small>停用后仍保留角色卡</small></span>
        </label>
        <div class="field field-span-2">
          <label for="character-profile">角色资料 JSON</label>
          <textarea id="character-profile" class="code-field">${escapeHTML(
            prettyJSON(item.profile),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="character-prompt">私有叙事方向</label>
          <textarea id="character-prompt" rows="7">${escapeHTML(
            item.prompt,
          )}</textarea>
          <small>可写动机、秘密与行为边界；玩家输入不能覆盖这里。</small>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("characters/save", {
        id: item.id,
        revision: item.revision,
        world_id: worldId,
        name: $("#character-name").value,
        slug: $("#character-slug").value,
        role: $("#character-role").value,
        sort_order: Number($("#character-order").value),
        enabled: $("#character-enabled").checked,
        profile: parseJSONField("#character-profile", "角色资料"),
        prompt: $("#character-prompt").value,
      });
      toast(character ? "角色卡已更新" : "角色卡已创建", "success");
      await loadCore();
      window.setTimeout(
        () => openCharacterManager(worldId).catch(showError),
        0,
      );
    },
  });
}

function openSessionCreator() {
  const availableWorlds = app.worlds.filter((world) => !world.archived);
  if (!availableWorlds.length) {
    toast("请先创建或恢复至少一个可用世界包", "error");
    switchView("worlds");
    return;
  }
  const preferredWorld =
    availableWorlds.find(
      (world) =>
        world.slug === app.settings?.runtime?.default_world_slug,
    ) || availableWorlds[0];
  openEditor({
    title: "建立群会话",
    kicker: "SESSION BINDING",
    body: `
      <div class="form-grid">
        <div class="binding-guide field-span-2">
          <strong>推荐自动绑定</strong>
          <span>
            只需先在“安全与设置”填写管理员 ID，再由该 ID
            在目标群发送 <code>/酒馆 开启</code>。系统会自动取得下面两个 ID，
            但不会直接启动剧情，而会先显示可选副本。
          </span>
        </div>
        <div class="field">
          <label for="session-platform">平台实例 ID（手动）</label>
          <input
            id="session-platform"
            placeholder="例如当前适配器的唯一实例 ID"
            autocomplete="off"
            spellcheck="false"
            required
          />
          <small>
            AstrBot 当前平台适配器实例的唯一 ID，不是机器人 QQ 号，也不只是平台类型名。
          </small>
        </div>
        <div class="field">
          <label for="session-group">群 ID（手动）</label>
          <input
            id="session-group"
            placeholder="QQ 群号或平台下发的群 OpenID"
            autocomplete="off"
            spellcheck="false"
            required
          />
          <small>
            NapCat / OneBot 通常是 QQ 群号；QQ 官方接入可能是群 OpenID。
          </small>
        </div>
        <div class="field field-span-2">
          <label for="session-world">世界包</label>
          <select id="session-world">
            ${availableWorlds
              .map(
                (world) =>
                  `<option value="${escapeHTML(world.id)}" ${
                    world.id === preferredWorld.id ? "selected" : ""
                  }>${escapeHTML(
                    world.name,
                  )}（${escapeHTML(world.slug)}）</option>`,
              )
              .join("")}
          </select>
          <small>WebUI 建立后默认为关闭状态，由管理员确认后开启。</small>
        </div>
        <div class="field">
          <label for="session-instance-name">副本名称</label>
          <input
            id="session-instance-name"
            value="${escapeHTML(preferredWorld.name)}"
            maxlength="100"
            required
          />
          <small>同一世界可以建立多个互不串档的副本。</small>
        </div>
        <div class="field">
          <label for="session-instance-slug">副本标识</label>
          <input
            id="session-instance-slug"
            value="${escapeHTML(preferredWorld.slug)}"
            maxlength="64"
            pattern="[a-z0-9][a-z0-9_\\-]{0,63}"
            required
          />
          <small>群内使用 /酒馆 开启 &lt;副本标识&gt; 选择。</small>
        </div>
      </div>
    `,
    saveLabel: "建立会话",
    onSave: async () => {
      const platformId = $("#session-platform").value.trim();
      const groupId = $("#session-group").value.trim();
      const instanceName = $("#session-instance-name").value.trim();
      const instanceSlug = $("#session-instance-slug").value.trim();
      if (!platformId || !groupId || !instanceName || !instanceSlug) {
        throw new Error(
          "手动建立副本时，平台实例 ID、群 ID、副本名称和副本标识都不能为空",
        );
      }
      await bridge.apiPost("sessions/action", {
        action: "create",
        platform_id: platformId,
        group_id: groupId,
        world_ref: $("#session-world").value,
        instance_name: instanceName,
        instance_slug: instanceSlug,
      });
      toast("群副本已建立，并已加入允许群列表", "success");
      await loadCore();
    },
  });

  $("#session-world").addEventListener("change", (event) => {
    const world = availableWorlds.find(
      (item) => item.id === event.currentTarget.value,
    );
    if (!world) return;
    $("#session-instance-name").value = world.name;
    $("#session-instance-slug").value = world.slug;
  });
}

async function runSessionAction(id, action, button = null) {
  const source = app.sessions.find((item) => item.id === id);
  if (action === "clone") {
    const detail = await bridge.apiGet("sessions/detail", { id });
    const suffix = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    openEditor({
      title: `克隆「${source?.instance_name || "副本"}」`,
      kicker: "BRANCH SESSION",
      body: `
        <div class="form-grid">
          <div class="field">
            <label for="clone-name">新副本名称</label>
            <input id="clone-name" maxlength="100"
              value="${escapeHTML(`${source?.instance_name || "副本"} · 分支`)}" />
          </div>
          <div class="field">
            <label for="clone-slug">新副本标识</label>
            <input id="clone-slug" maxlength="64"
              value="${escapeHTML(`${source?.instance_slug || "session"}-branch-${suffix}`)}" />
          </div>
          <div class="field field-span-2">
            <label for="clone-snapshot">分支起点</label>
            <select id="clone-snapshot">
              <option value="">当前状态；已归档副本自动使用最终存档</option>
              ${(detail.snapshots || [])
                .map(
                  (item) =>
                    `<option value="${escapeHTML(item.id)}">${escapeHTML(
                      item.name,
                    )} · 第 ${escapeHTML(item.turn_no)} 回合</option>`,
                )
                .join("")}
            </select>
            <small>新副本会复制世界状态、动态 NPC、剧情账本、场景时钟与长期记忆；原副本不受影响。</small>
          </div>
        </div>
      `,
      saveLabel: "创建分支",
      onSave: async () => {
        await bridge.apiPost("sessions/action", {
          action: "clone",
          session_id: id,
          instance_name: $("#clone-name").value.trim(),
          instance_slug: $("#clone-slug").value.trim(),
          snapshot_ref: $("#clone-snapshot").value,
        });
        toast("分支副本已建立，当前为关闭状态", "success");
        await loadCore();
      },
    });
    return;
  }
  let reason = "";
  if (action === "finish") {
    const ok = await confirmAction(
      "永久完结这个故事？",
      "系统会创建最终保护存档、取消全部计时与临时权限，并把副本设为只读。以后请从最终存档克隆续作。",
      "确认完结",
    );
    if (!ok) return;
  }
  if (action === "abort") {
    reason = await promptForText({
      title: "强制终止原因",
      kicker: "ABORT SESSION",
      label: "终止原因（必填）",
      placeholder: "例如：团内争议、连续故障",
      required: true,
    });
    if (!reason) return;
    const ok = await confirmAction(
      "强制终止并永久归档？",
      `原因：${reason}`,
      "确认终止",
    );
    if (!ok) return;
  }
  const execute = async () => {
    const session = app.sessions.find((item) => item.id === id);
    await bridge.apiPost("sessions/action", {
      session_id: id,
      action,
      reason,
      resume: action === "perform" && Number(session?.turn_no || 0) > 0,
    });
    toast(
      `会话已${
        {
          start: "进入准备大厅",
          perform: "正式推进",
          resume: "恢复至准备大厅",
          pause: "暂停",
          close: "关闭",
          finish: "永久完结并归档",
          abort: "强制终止并归档",
        }[action]
      }`,
      "success",
    );
    await loadCore();
  };
  if (action === "close") {
    const ok = await confirmAction(
      "关闭酒馆会话？",
      "关闭后不再处理群消息，也不会调用模型；现有状态和记忆仍会保留。",
      "关闭会话",
    );
    if (!ok) return;
  }
  await withBusy(button, execute);
}

async function openSessionDetail(sessionId) {
  const detail = await bridge.apiGet("sessions/detail", { id: sessionId });
  app.currentSession = detail;
  const session = detail.session;
  const roster = detail.roster || [];
  const timing = detail.instance_config?.time_rules || {};
  const ruleState = detail.rule_state || {};
  const controlState = detail.control || { mode: "auto", phase: "auto", beat_no: 0 };
  const progress = ruleState.progress || {};
  const sessionCharacters = detail.session_characters || [];
  // A11：世界状态可视化所需的 ID → 名称映射（参与者 / NPC）。
  const wsIdLabels = Object.assign(
    {},
    detail.session?.id_labels || detail.id_labels || {},
  );
  (detail.roster || []).forEach((item) => {
    if (item.id) {
      wsIdLabels[item.id] =
        item.character_name || item.display_name || item.id;
    }
    if (item.group_user_id && item.character_name) {
      wsIdLabels[item.group_user_id] = item.character_name;
    }
  });
  (sessionCharacters || []).forEach((item) => {
    if (item.id) wsIdLabels[item.id] = item.name || item.id;
    if (item.stable_key && item.name) wsIdLabels[item.stable_key] = item.name;
    if (item.name) {
      wsIdLabels[item.name] = item.name;
      wsIdLabels[`npc:${item.name}`] = item.name;
    }
  });
  wsIdLabels["队伍"] = "队伍";
  wsIdLabels["party"] = "队伍";
  const memories = detail.memories || [];
  const storage = detail.storage || {};
  const operations = detail.operations || [];
  const cardRevisions = detail.card_revisions || [];
  const timerPolicy = detail.timer_policy || {
    global_enabled: false,
    switches: {},
    effective: {},
  };
  const tokenUsage = detail.token_usage || {
    session: { hour: 0, day: 0, all: 0 },
    group: { hour: 0, day: 0, all: 0 },
    quotas: [],
    by_type: [],
  };
  const sessionQuota =
    tokenUsage.quotas.find((item) => item.scope_type === "session") || {};
  const quotaUsed = sessionQuota.enabled
    ? Number(tokenUsage.session?.hour || 0)
    : 0;
  const quotaLimit = sessionQuota.enabled
    ? Number(sessionQuota.token_limit || 0)
    : 0;
  const pct = quotaLimit > 0 ? Math.min(100, Math.round((quotaUsed / quotaLimit) * 100)) : 0;
  const characterCardTemplate = resolvedCharacterCardTemplate(detail);
  const readonly = Boolean(detail.archive?.readonly);
  const turn = detail.turn || {
    round_no: 1,
    current_user_id: "",
    current_name: "",
    order: [],
  };
  const currentActor =
    turn.current_name || turn.current_user_id || "等待玩家加入";
  const nullable = (value) => (value === null || value === undefined ? "" : value);
  $("#session-modal-title").textContent =
    `${session.group_remark || `群 ${session.group_id}`} · ${
      session.instance_name || session.world_name
    }`;
  $("#session-modal-body").innerHTML = `
    <div class="detail-grid">
      <div class="detail-card detail-icon-card"><span class="dc-icon">⚡</span><div class="dc-body"><span class="dc-label">运行状态</span><strong>${escapeHTML(
        statusLabel(session.state),
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🔖</span><div class="dc-body"><span class="dc-label">副本标识</span><strong>${escapeHTML(
        session.instance_slug || session.world_slug,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🌍</span><div class="dc-body"><span class="dc-label">世界包</span><strong>${escapeHTML(
        session.world_name,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🔢</span><div class="dc-body"><span class="dc-label">剧情回合</span><strong>${escapeHTML(
        session.turn_no,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🔁</span><div class="dc-body"><span class="dc-label">多人轮次</span><strong>第 ${escapeHTML(
        turn.round_no,
      )} 轮</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🎭</span><div class="dc-body"><span class="dc-label">当前行动者</span><strong>${escapeHTML(
        currentActor,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">📍</span><div class="dc-body"><span class="dc-label">当前位置</span><strong>${escapeHTML(
        session.world_state?.location || "未记录",
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🧾</span><div class="dc-body"><span class="dc-label">状态修订</span><strong>r${escapeHTML(
        session.revision,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">📖</span><div class="dc-body"><span class="dc-label">当前章节</span><strong>${escapeHTML(
        progress.chapter || "未设置",
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🎯</span><div class="dc-body"><span class="dc-label">剧情目标</span><strong>${escapeHTML(
        progress.current_objective || "未设置",
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🧠</span><div class="dc-body"><span class="dc-label">长期记忆</span><strong>${escapeHTML(
        memories.length,
      )}</strong></div></div>
      <div class="detail-card detail-icon-card"><span class="dc-icon">🧝</span><div class="dc-body"><span class="dc-label">副本 NPC</span><strong>${escapeHTML(
        sessionCharacters.length,
      )}</strong></div></div>
    </div>
    ${
      detail.archive
        ? `<div class="notice notice-warning archive-notice">
            <div><strong>永久只读归档</strong><span>${escapeHTML(
              detail.archive.termination_type === "aborted"
                ? `强制终止：${detail.archive.reason}`
                : "故事正常完结",
            )} · ${escapeHTML(formatDate(detail.archive.ended_at))}</span></div>
          </div>`
        : ""
    }
    <section class="narrative-control-card ${controlState.mode === "dm" ? "is-dm" : ""}" style="margin-top:16px">
      <div class="nc-head">
        <span class="nc-icon" aria-hidden="true">${controlState.mode === "dm" ? "🎮" : "🤖"}</span>
        <div class="nc-title">
          <div class="eyebrow">NARRATIVE CONTROL</div>
          <h2>叙事控制：${controlState.mode === "dm" ? "DM 主持" : "AI 自动"}</h2>
        </div>
        <span class="status-badge ${controlState.mode === "dm" ? "status-paused" : "status-running"}">${
          controlState.mode === "dm" ? "真人主持" : "AI 自动"
        }</span>
      </div>
      <div class="nc-meta">
        <span>👤 活动 DM：${escapeHTML(controlState.active_dm_user_id || "无")}</span>
        <span>🧭 阶段：${escapeHTML(controlState.phase || "auto")}</span>
        <span>📈 已推进 ${escapeHTML(controlState.beat_no || 0)} 段</span>
      </div>
      <div class="nc-actions">
        <button class="action-button" data-session-detail-action="dm-enable" ${readonly ? "disabled" : ""}>开启/接管</button>
        <button class="action-button" data-session-detail-action="dm-directive" ${controlState.mode !== "dm" || readonly ? "disabled" : ""}>一次性指引</button>
        <button class="action-button" data-session-detail-action="dm-direct" ${controlState.mode !== "dm" || readonly ? "disabled" : ""}>原文插入</button>
        <button class="action-button is-danger" data-session-detail-action="dm-disable" ${controlState.mode !== "dm" || readonly ? "disabled" : ""}>恢复自动</button>
      </div>
      <p class="nc-hint">AI 辅助推进和玩家/NPC 交棒使用群命令；这里与同一主持状态、存档和审计链联动。</p>
    </section>
    <div class="tabbar">
      <button class="tab-button is-active" data-session-tab="state">总览与规则</button>
      <button class="tab-button" data-session-tab="roster">准备与角色 ${escapeHTML(
        roster.length,
      )}</button>
      <button class="tab-button" data-session-tab="npcs">NPC ${escapeHTML(
        sessionCharacters.length,
      )}</button>
      <button class="tab-button" data-session-tab="memory">长期记忆 ${escapeHTML(
        memories.length,
      )}</button>
      <button class="tab-button" data-session-tab="timing">时间与流程 ${escapeHTML(
        (detail.timers || []).filter((item) => ["active", "paused"].includes(item.status))
          .length,
      )}</button>
      <button class="tab-button" data-session-tab="rescue">急救与诊断 ${escapeHTML(
        operations.filter((item) => item.status === "pending").length,
      )}</button>
      <button class="tab-button" data-session-tab="access">权限与封禁 ${escapeHTML(
        (detail.bans || []).length,
      )}</button>
      <button class="tab-button" data-session-tab="saves">存档 ${escapeHTML(
        detail.snapshots.length,
      )}</button>
      <button class="tab-button" data-session-tab="events">时间线 ${escapeHTML(
        detail.events.length,
      )}</button>
    </div>
    <div class="tab-panel is-active" data-session-tab-panel="state">
      <div class="field">
        <label for="session-state-json">受控世界状态 JSON</label>
        <textarea id="session-state-json" class="code-field" rows="18">${escapeHTML(
          prettyJSON(session.world_state),
        )}</textarea>
        <small>可视化视图已迁移至「副本实时仪表盘 → 受控世界状态」；此 JSON 为唯一手工编辑入口，保存前会自动生成安全快照；权限与会话状态不在此对象内。</small>
      </div>
      <div class="field" style="margin-top:18px">
        <label for="session-rules-json">副本规则、进度与内容边界 JSON</label>
        <textarea id="session-rules-json" class="code-field" rows="18">${escapeHTML(
          prettyJSON(ruleState),
        )}</textarea>
        <small>进度百分比只按正式里程碑计算；骰点可见性、NPC 策略和上下文预算也在这里按副本覆盖。</small>
      </div>
      <div class="modal-foot" style="margin: 18px -21px -20px">
        <button class="button button-primary" data-session-detail-action="save-state" ${
          readonly ? "disabled" : ""
        }>
          保存状态
        </button>
        <button class="button button-primary" data-session-detail-action="save-rules" ${
          readonly ? "disabled" : ""
        }>
          保存副本规则
        </button>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="npcs">
      <div class="tab-section-card">
        <span class="is-icon" aria-hidden="true">🧝</span>
        <div class="is-main">
          <strong>NPC</strong>
          <small>世界预设与剧情中途登记的 NPC 都保存在当前副本，不会污染其他副本。</small>
        </div>
        <div class="toolbar-actions">
          <button class="button button-primary" data-session-detail-action="new-npc" ${
            readonly ? "disabled" : ""
          }>＋ 添加 NPC</button>
        </div>
      </div>
      <div class="tab-card">
        <div class="session-stack">
          ${
            sessionCharacters.length
              ? sessionCharacters
                  .map(
                    (item) => `<div class="session-row">
                      <span class="row-icon" aria-hidden="true">🧝</span>
                      <div><div class="session-name">${escapeHTML(item.name)}</div>
                      <div class="session-meta">${escapeHTML(item.role_type)} · ${escapeHTML(
                        item.source,
                      )} · ${escapeHTML(item.review_status)}</div></div>
                      <div class="session-location">${escapeHTML(
                        item.state?.location || item.lifecycle_status,
                      )}</div>
                      <button class="action-button" data-session-detail-action="edit-npc"
                        data-id="${escapeHTML(item.id)}" ${readonly ? "disabled" : ""}>编辑</button>
                    </div>`,
                  )
                  .join("")
              : '<div class="empty-state compact"><span>当前副本尚无登记 NPC</span></div>'
          }
        </div>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="memory">
      <div class="tab-section-card">
        <span class="is-icon" aria-hidden="true">🧠</span>
        <div class="is-main">
          <strong>长期记忆</strong>
          <small>记忆可锁定、置顶、设为主持人或个人可见，并可标记失效或被新事实替代。</small>
        </div>
        <div class="toolbar-actions">
          <button class="button button-primary" data-session-detail-action="new-memory" ${
            readonly ? "disabled" : ""
          }>＋ 添加记忆</button>
        </div>
      </div>
      <div class="memory-list">
        ${
          memories.length
            ? memories
                .map(
                  (memory) => `<article class="memory-row ${
                    memory.invalidated ? "is-invalidated" : ""
                  }">
                    <span class="row-icon" aria-hidden="true">${
                      { 地点: "📍", npc: "🧝", 事件: "📜", fact: "✦" }[
                        memory.kind
                      ] || "🧠"
                    }</span>
                    <div class="memory-importance"><span>${escapeHTML(
                      memory.importance,
                    )}</span></div>
                    <div class="memory-main">
                      <div class="memory-row-head"><span class="memory-kind">${escapeHTML(
                        memory.scope,
                      )} · ${escapeHTML(memory.kind)} · ${escapeHTML(
                        memory.visibility,
                      )}</span></div>
                      <div class="memory-content">${escapeHTML(memory.content)}</div>
                      <div class="tag-row">${memory.locked ? "<span>锁定</span>" : ""}${
                        memory.pinned ? "<span>置顶</span>" : ""
                      }${memory.invalidated ? "<span>已失效</span>" : ""}</div>
                    </div>
                    <button class="action-button" data-session-detail-action="edit-memory"
                      data-id="${escapeHTML(memory.id)}" ${
                        readonly ? "disabled" : ""
                      }>编辑</button>
                  </article>`,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前副本尚无长期记忆</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="roster">
      <div class="tab-section-card">
        <span class="is-icon" aria-hidden="true">🧑‍🤝‍🧑</span>
        <div class="is-main">
          <strong>准备与角色</strong>
          <small>每名玩家均显示完整角色资料、属性修正、当前副本状态与审核记录；点击卡片标题可收起或展开。</small>
        </div>
        <div class="toolbar-actions">
          <span class="status-badge status-${escapeHTML(session.state)}">${escapeHTML(
            detail.preflight?.ok ? "可开演" : `${detail.preflight?.blockers?.length || 0} 项阻塞`,
          )}</span>
          <button class="button button-primary" data-session-detail-action="force-ready" ${
            readonly || session.state !== "preparing" ? "disabled" : ""
          }>强制所有合格角色准备</button>
        </div>
      </div>
      <div class="roster-character-list">
        ${
          roster.length
            ? roster
                .map(
                  (item, index) =>
                    renderRosterCharacterCard(
                      item,
                      characterCardTemplate,
                      session,
                      readonly,
                      index,
                    ),
                )
                .join("")
            : '<div class="empty-state compact"><span>尚未有玩家预留席位；请在群内发送 /酒馆 加入。</span></div>'
        }
      </div>
      ${
        cardRevisions.length
          ? `<div class="panel" style="margin-top:18px">
              <div class="panel-head">
                <div><div class="eyebrow">✍️ CARD REVISION REVIEW</div>
                  <h2>角色卡修改申请 ${escapeHTML(
                    cardRevisions.filter((item) => item.status === "pending").length,
                  )} 项待审核</h2></div>
                <span class="status-badge">共 ${escapeHTML(
                  cardRevisions.length,
                )} 项</span>
              </div>
              <div class="revision-reviews">
                ${cardRevisions
                  .map(
                    (item) => `
                      <div class="revision-row">
                        <div class="revision-main">
                          <div class="revision-title">${escapeHTML(
                            item.character_name || item.display_name,
                          )} · v${escapeHTML(item.base_version)} → v${escapeHTML(
                            item.candidate_version,
                          )}</div>
                          <div class="revision-meta">${escapeHTML(
                            item.request_note || "未填写修改说明",
                          )} · ${
                            item.status === "pending"
                              ? "待审核"
                              : item.status === "approved"
                                ? "已通过"
                                : item.status === "rejected"
                                  ? "已拒绝"
                                  : escapeHTML(item.status)
                          } · ${escapeHTML(formatDate(item.updated_at))}</div>
                        </div>
                        ${
                          item.status === "pending"
                            ? `<div class="table-actions">
                                <button class="action-button" data-session-detail-action="revision-approve" data-request-id="${escapeHTML(
                                  item.id,
                                )}" ${readonly ? "disabled" : ""}>通过</button>
                                <button class="action-button is-danger" data-session-detail-action="revision-reject" data-request-id="${escapeHTML(
                                  item.id,
                                )}" ${readonly ? "disabled" : ""}>拒绝</button>
                              </div>`
                            : `<span class="status-badge ${
                                item.status === "approved"
                                  ? "status-running"
                                  : item.status === "rejected"
                                    ? "status-maintenance"
                                    : ""
                              }">${
                                item.status === "pending"
                                  ? "待审核"
                                  : item.status === "approved"
                                    ? "已通过"
                                    : item.status === "rejected"
                                      ? "已拒绝"
                                      : "已取消"
                              }</span>`
                        }
                      </div>`,
                  )
                  .join("")}
              </div>
              <p class="field-hint" style="margin-top:10px">改名 / 编辑角色卡提交的修改都会在这里审核；通过后角色名与角色卡同步生效。</p>
            </div>`
          : ""
      }
      ${
        detail.preflight?.blockers?.length
          ? `<div class="notice notice-warning" style="margin-top:16px">
              <div><strong>开演阻塞</strong><span>${detail.preflight.blockers
                .map((item) => escapeHTML(item))
                .join("；")}</span></div>
            </div>`
          : ""
      }
    </div>
    <div class="tab-panel" data-session-tab-panel="timing">
      <div class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><div class="eyebrow">⏱ COUNTDOWN POLICY</div>
          <h2>倒计时总开关与分类开关</h2></div>
          <button class="button button-primary" data-timer-policy="all"
            data-enabled="${timerPolicy.global_enabled ? "false" : "true"}" ${
              readonly ? "disabled" : ""
            }>${timerPolicy.global_enabled ? "关闭全部倒计时" : "启用全部倒计时"}</button>
        </div>
        <div class="tag-row">
          ${[
            ["card_code", "建卡码"],
            ["card_completion", "角色卡完成"],
            ["preparation", "准备大厅"],
            ["ready", "准备确认"],
            ["turn", "行动回合"],
            ["vote", "集体投票"],
            ["standby", "候补等待"],
            ["all_idle", "全员无互动"],
          ]
            .map(
              ([key, label]) => `<button class="action-button ${
                timerPolicy.effective?.[key] ? "" : "is-danger"
              }" data-timer-policy="${key}"
                data-enabled="${timerPolicy.switches?.[key] === false ? "true" : "false"}"
                ${readonly ? "disabled" : ""}>${escapeHTML(label)}：${
                  timerPolicy.effective?.[key] ? "开" : "关"
                }</button>`,
            )
            .join("")}
        </div>
        <p>关闭会冻结对应计时器并保存真实剩余时间；重新开启后继续，不执行停用期间的超时处罚。</p>
      </div>
            <div class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><div class="eyebrow">🪙 TOKEN BUDGET</div>
          <h2>当前副本 Token 用量与滚动限额</h2></div>
          <button class="button button-primary" data-session-detail-action="save-session-token-quota" ${
            readonly ? "disabled" : ""
          }>保存副本限额</button>
        </div>
        <div class="tb-status-grid">
          <div class="tb-stat"><span>当前用量（本窗口）</span><strong>${escapeHTML(
            formatTokens(tokenUsage.session.hour),
          )}</strong></div>
          <div class="tb-stat"><span>24 小时用量</span><strong>${escapeHTML(
            formatTokens(tokenUsage.session.day),
          )}</strong></div>
          <div class="tb-stat"><span>副本累计</span><strong>${escapeHTML(
            formatTokens(tokenUsage.session.all),
          )}</strong></div>
          <div class="tb-stat ${pct >= 90 ? "is-warn" : ""}"><span>副本软限额</span><strong>${
            sessionQuota.enabled
              ? escapeHTML(formatTokens(sessionQuota.token_limit))
              : "未启用"
          }</strong></div>
          <div class="tb-stat"><span>剩余容量（本窗口）</span><strong>${
            sessionQuota.enabled
              ? escapeHTML(
                  formatTokens(
                    Math.max(0, sessionQuota.token_limit - (tokenUsage.session.hour || 0)),
                  ),
                )
              : "—"
          }</strong></div>
          <div class="tb-stat"><span>最近裁剪</span><strong>${escapeHTML(
            tokenUsage.last_trim_at || "—",
          )}</strong></div>
          <div class="tb-stat"><span>上下文预算</span><strong>${escapeHTML(
            tokenUsage.context_budget?.recent_turns ?? "—",
          )}</strong></div>
        </div>
        <div class="tb-meter" style="margin-top:12px">
          <i style="width:${pct}%" class="${pct >= 90 ? "danger" : pct >= 70 ? "warn" : ""}"></i>
        </div>
        <p class="field-hint" style="margin:6px 0 0">本窗口占用 ${pct}%${
          pct >= 90 ? " · 已接近/超过限额，将触发上下文裁剪与提示" : ""
        }</p>
        <div class="tb-section">
          <div class="tb-section-head">⚙️ 常用控制</div>
          <div class="form-grid">
            <label class="switch-field"><input id="quota-session-enabled" type="checkbox" ${
              sessionQuota.enabled ? "checked" : ""
            } /><span><strong>启用副本限额</strong><small>只限制当前故事副本</small></span></label>
            <div class="field"><label for="quota-session-window">副本滚动窗口（秒）</label>
              <input id="quota-session-window" type="number" min="60"
                value="${escapeHTML(sessionQuota.window_seconds || 3600)}" /></div>
            <div class="field"><label for="quota-session-limit">副本 Token 上限</label>
              <input id="quota-session-limit" type="number" min="1"
                value="${escapeHTML(sessionQuota.token_limit || 100000)}" /></div>
          </div>
        </div>
        <details class="tb-section">
          <summary class="tb-section-head" style="cursor:pointer">🧩 高级配置与调试</summary>
          <p class="field-hint">滚动上下文、裁剪阈值与摘要策略由世界包 rules.context_budget 与全局配置决定；此处只读展示当前生效值。</p>
          <div class="tb-status-grid" style="margin-top:8px">
            <div class="tb-stat"><span>最近回合</span><strong>${escapeHTML(
              String(app.settings?.runtime?.recent_turns ?? "—"),
            )}</strong></div>
            <div class="tb-stat"><span>记忆条数</span><strong>${escapeHTML(
              String(app.settings?.runtime?.memory_limit ?? "—"),
            )}</strong></div>
            <div class="tb-stat"><span>最大输出</span><strong>${escapeHTML(
              String(app.settings?.model?.max_tokens ?? "—"),
            )}</strong></div>
            <div class="tb-stat"><span>按类型明细</span><strong>${escapeHTML(
              String((tokenUsage.by_type || []).length) + " 类",
            )}</strong></div>
          </div>
        </details>
        <div class="tb-section">
          <div class="tb-section-head">⚠️ 危险操作</div>
          <div class="tb-danger">
            <button class="action-button is-danger" type="button"
              data-session-detail-action="token-danger" data-danger="reset">重置 Token 统计</button>
          </div>
          <p class="field-hint" style="margin-top:6px">重置 Token 统计不会删除任何剧情。裁剪与摘要由插件上下文管理自动执行，不提供手动覆盖。</p>
        </div>
        <p style="margin-top:12px">群级 Token 限额已移到群会话标题旁的“群 Token 限额”，不会再跟随某个副本详情修改。</p>
      </div>

      ${renderEconomyPanel(detail)}
<div class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><div class="eyebrow">⏲ TIME & FLOW</div>
          <h2>副本时间与流程</h2></div>
          <button class="button button-primary" data-session-detail-action="save-timing" ${
            readonly ? "disabled" : ""
          }>保存副本时间规则</button>
        </div>
        <p class="field-hint" style="margin-bottom:12px">留空表示不限时。副本值是创建时快照，修改世界模板不会突变正在运行的团。</p>
      <div class="form-grid">
        <div class="field"><label for="t-card-code">建卡码有效期</label>
          <input id="t-card-code" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_code_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-card-draft">草稿保留</label>
          <input id="t-card-draft" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_draft_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-card-completion">等待完成建卡</label>
          <input id="t-card-completion" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_completion_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-preparation">准备大厅上限</label>
          <input id="t-preparation" type="number" min="1" value="${escapeHTML(
            nullable(timing.preparation_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-ready">准备确认</label>
          <input id="t-ready" type="number" min="1" value="${escapeHTML(
            nullable(timing.ready_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn">个人回合</label>
          <input id="t-turn" type="number" min="1" value="${escapeHTML(
            nullable(timing.turn_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn-reminder">回合提前提醒</label>
          <input id="t-turn-reminder" type="number" min="1" value="${escapeHTML(
            nullable(timing.turn_reminder_seconds),
          )}" /></div>
        <div class="field"><label for="t-timeout-count">连续超时转候补次数</label>
          <input id="t-timeout-count" type="number" min="-1" max="20"
            value="${escapeHTML(timing.max_consecutive_timeouts ?? 2)}" /></div>
        <div class="field"><label for="t-standby">候补自动退场</label>
          <input id="t-standby" type="number" min="1" value="${escapeHTML(
            nullable(timing.standby_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-delegation">代控授权有效期</label>
          <input id="t-delegation" type="number" min="1" value="${escapeHTML(
            nullable(timing.delegation_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-all-idle">全员无互动暂停</label>
          <input id="t-all-idle" type="number" min="1" value="${escapeHTML(
            nullable(timing.all_idle_pause_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-one">第一轮投票</label>
          <input id="t-vote-one" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_round_one_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-two">第二轮投票</label>
          <input id="t-vote-two" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_round_two_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-reminder">投票提前提醒</label>
          <input id="t-vote-reminder" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_reminder_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn-action">回合超时处理</label>
          <select id="t-turn-action">
            <option value="skip" ${timing.turn_timeout_action !== "hold" ? "selected" : ""}>跳过并累计超时</option>
            <option value="hold" ${timing.turn_timeout_action === "hold" ? "selected" : ""}>保留行动权</option>
          </select></div>
        <div class="field"><label for="t-card-action">建卡超时处理</label>
          <select id="t-card-action">
            <option value="standby" ${timing.card_timeout_action === "standby" ? "selected" : ""}>转候补</option>
            <option value="release" ${timing.card_timeout_action === "release" ? "selected" : ""}>释放席位</option>
            <option value="remind" ${timing.card_timeout_action === "remind" ? "selected" : ""}>仅提醒</option>
          </select></div>
        <div class="field"><label for="t-ready-action">准备超时处理</label>
          <select id="t-ready-action">
            <option value="standby" ${timing.ready_timeout_action === "standby" ? "selected" : ""}>转候补</option>
            <option value="remind" ${timing.ready_timeout_action === "remind" ? "selected" : ""}>仅提醒</option>
          </select></div>
        <label class="switch-field"><input id="t-pause-clock" type="checkbox" ${
          timing.pause_stops_clock !== false ? "checked" : ""
        } /><span><strong>暂停期间停止计时</strong><small>恢复后接着剩余时间</small></span></label>
        <label class="switch-field"><input id="t-announce" type="checkbox" ${
          timing.announce_timeouts !== false ? "checked" : ""
        } /><span><strong>在群内公布超时结果</strong><small>提醒和处理结果会发往对应会话</small></span></label>
      </div>
      </div>
      <div class="session-stack" style="margin-top:18px">
        ${
          (detail.timers || []).length
            ? detail.timers
                .slice(0, 30)
                .map(
                  (timer) => `
                    <div class="session-row">
                      <span class="row-icon" aria-hidden="true">⏱</span>
                      <div><div class="session-name">${escapeHTML(
                        timer.character_name || timer.display_name || timer.timer_type,
                      )}</div><div class="session-meta">${escapeHTML(
                        timer.timer_type,
                      )} · ${escapeHTML(timer.status)}</div></div>
                      <div class="session-location">${escapeHTML(
                        timer.deadline_at || "无截止时间",
                      )}</div>
                      <div class="table-actions">
                        ${
                          timer.status === "active"
                            ? `<button class="action-button" data-timer-action="pause"
                                data-id="${escapeHTML(timer.id)}">暂停</button>`
                            : ""
                        }
                        ${
                          timer.status === "paused"
                            ? `<button class="action-button" data-timer-action="resume"
                                data-id="${escapeHTML(timer.id)}">恢复</button>`
                            : ""
                        }
                        ${
                          ["active", "paused"].includes(timer.status)
                            ? `<button class="action-button" data-timer-action="extend"
                                data-id="${escapeHTML(timer.id)}">延长</button>
                              <button class="action-button is-danger" data-timer-action="expire"
                                data-id="${escapeHTML(timer.id)}">立即到期</button>
                              <button class="action-button is-danger" data-timer-action="disable"
                                data-id="${escapeHTML(timer.id)}">关闭限制</button>`
                            : ""
                        }
                      </div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前没有计时器</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="access">
      <div class="tab-section-card">
        <span class="is-icon" aria-hidden="true">🔑</span>
        <div class="is-main">
          <strong>权限与封禁</strong>
          <small>主持人负责副本流程；秩序管理员只处理队列与副本级异常。</small>
        </div>
        <div class="toolbar-actions">
          <input id="permission-user-id" placeholder="真实用户 ID" />
          <select id="permission-role"><option value="moderator">秩序管理员</option>
            <option value="host">副本主持人</option></select>
          <button class="button button-primary" data-session-detail-action="grant-role">
            授予权限
          </button>
        </div>
      </div>
      <div class="tab-card">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">🔑</span><strong>副本角色权限</strong></div>
        <div class="session-stack">
          ${(detail.permissions || [])
            .map(
              (item) => `<div class="session-row">
                <span class="row-icon" aria-hidden="true">🔑</span><div><div class="session-name">
                ${escapeHTML(item.user_id)}</div><div class="session-meta">由
                ${escapeHTML(item.granted_by)} 授予</div></div>
                <div class="session-location">${escapeHTML(item.role)}</div></div>`,
            )
            .join("") || '<div class="empty-state compact"><span>尚未设置副本角色权限</span></div>'}
        </div>
      </div>
      <div class="tab-card" style="margin-top:14px">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">🚫</span><strong>有效黑名单</strong></div>
        <div class="session-stack">
          ${(detail.bans || [])
            .map(
              (item) => `<div class="session-row">
                <span class="row-icon" aria-hidden="true">🚫</span><div><div class="session-name">
                ${escapeHTML(item.user_id)}</div><div class="session-meta">
                ${escapeHTML(item.reason || "未注明原因")}</div></div>
                <div class="session-location">${escapeHTML(item.scope)} ·
                ${escapeHTML(item.expires_at || "永久")}</div></div>`,
            )
            .join("") || '<div class="empty-state compact"><span>当前没有有效封禁</span></div>'}
        </div>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="rescue">
      <div class="tab-section-card">
        <span class="is-icon" aria-hidden="true">🩹</span>
        <div class="is-main">
          <strong>急救与诊断</strong>
          <small>只修复当前损坏的组件；所有操作都会写入审计记录。运行中的世界契约不会被热替换。</small>
        </div>
        <div class="toolbar-actions">
          <button class="button button-primary" data-session-detail-action="download-diagnostics">导出脱敏诊断包</button>
        </div>
      </div>
      <div class="detail-grid">
        <div class="detail-card detail-icon-card"><span class="dc-icon">📌</span><div class="dc-body"><span class="dc-label">未完成事务</span><strong>${escapeHTML(operations.filter((item) => item.status === "pending").length)}</strong></div></div>
        <div class="detail-card detail-icon-card"><span class="dc-icon">❌</span><div class="dc-body"><span class="dc-label">失败事务</span><strong>${escapeHTML(operations.filter((item) => item.status === "failed").length)}</strong></div></div>
        <div class="detail-card detail-icon-card"><span class="dc-icon">✍️</span><div class="dc-body"><span class="dc-label">角色卡修订</span><strong>${escapeHTML(cardRevisions.filter((item) => item.status === "pending").length)}</strong></div></div>
      </div>
      <div class="tab-card" style="margin-top:14px">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">🔧</span><strong>事务恢复</strong></div>
        <div class="session-stack">
          ${operations.slice(0, 12).map((item) => `<div class="session-row"><span class="row-icon" aria-hidden="true">🔧</span><div><div class="session-name">${escapeHTML(item.operation_type)} · ${escapeHTML(item.status)}</div><div class="session-meta">${escapeHTML(item.operation_id)} · ${escapeHTML(item.result?.phase || "reserved")}</div></div>${item.status === "pending" ? `<button class="action-button is-danger" data-session-detail-action="cancel-operation" data-operation-id="${escapeHTML(item.operation_id)}">放弃任务</button>` : ""}</div>`).join("") || '<div class="empty-state compact"><span>暂无事务记录</span></div>'}
        </div>
      </div>
      <div class="field" style="margin-top:22px">
        <label for="rescue-choices-json">当前 A—D 选项 JSON</label>
        <textarea id="rescue-choices-json" class="code-field" rows="14">${escapeHTML(prettyJSON(detail.choice?.choices || []))}</textarea>
        <small>危险度与检定仍会按照冻结的世界契约严格校验。</small>
      </div>
      <button class="button button-primary" data-session-detail-action="replace-choices" ${!detail.choice || readonly ? "disabled" : ""}>保存当前选项</button>
      <div class="field" style="margin-top:22px">
        <label for="rescue-narrative">故事正文／过渡剧情</label>
        <textarea id="rescue-narrative" rows="8" placeholder="仅填写需要修订或插入的故事正文"></textarea>
      </div>
      <div class="toolbar-actions">
        <button class="button" data-session-detail-action="edit-last-narrative" ${readonly ? "disabled" : ""}>修订上一段正文</button>
        <button class="button" data-session-detail-action="bridge-narrative" ${readonly ? "disabled" : ""}>插入过渡剧情</button>
        <button class="button button-danger" data-session-detail-action="rollback-before-turn" ${readonly ? "disabled" : ""}>回到本轮之前</button>
      </div>
      <div class="tab-card" style="margin-top:14px">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">✍️</span><strong>角色卡修改申请</strong></div>
        <div class="session-stack">
          ${cardRevisions.map((item) => `<div class="session-row"><span class="row-icon" aria-hidden="true">✍️</span><div><div class="session-name">${escapeHTML(item.character_name || item.display_name)} · v${escapeHTML(item.base_version)} → v${escapeHTML(item.candidate_version)}</div><div class="session-meta">${escapeHTML(item.request_note || "未填写修改说明")} · ${escapeHTML(item.status)}</div></div>${item.status === "pending" ? `<div class="table-actions"><button class="action-button" data-session-detail-action="revision-approve" data-request-id="${escapeHTML(item.id)}">通过</button><button class="action-button is-danger" data-session-detail-action="revision-reject" data-request-id="${escapeHTML(item.id)}">拒绝</button></div>` : ""}</div>`).join("") || '<div class="empty-state compact"><span>暂无角色卡修改申请</span></div>'}
        </div>
      </div>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="saves">
      <div class="storage-summary">
        <div>
          <span>📁 副本独立目录</span>
          <code>${escapeHTML(storage.relative_path || "等待建立目录")}</code>
        </div>
        <div>
          <span>🗄️ 实时运行库</span>
          <strong>${storage.database_exists ? "instance.sqlite3 已就绪" : "等待同步"}</strong>
        </div>
        <div>
          <span>💾 独立文件存档</span>
          <strong>${escapeHTML(storage.save_files?.length || 0)} 份手动／最终存档 ·
            ${escapeHTML(storage.backup_files?.length || 0)} 份安全备份</strong>
        </div>
        <div>
          <span>🔁 文件同步</span>
          <strong>${escapeHTML(storage.sync_status || "pending")}</strong>
        </div>
      </div>
      <div class="tab-card" style="margin-top:14px">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">🗂️</span><strong>独立安全文件</strong></div>
        <div class="session-stack">
        ${
          [
            ...(storage.save_files || []).map((item) => ({
              ...item,
              label: "手动／最终存档",
            })),
            ...(storage.backup_files || []).map((item) => ({
              ...item,
              label: "周期安全备份",
            })),
          ]
            .map(
              (item) => `<div class="session-row">
                <span class="row-icon" aria-hidden="true">${item.kind === "save" ? "💾" : "🗜️"}</span><div>
                <div class="session-name"><code>${escapeHTML(
                  item.filename,
                )}</code></div>
                <div class="session-meta">${escapeHTML(item.label)} ·
                  ${escapeHTML(formatDate(item.created_at))}</div>
              </div><div class="session-location">${escapeHTML(
                formatBytes(item.size || 0),
              )}</div>${
                item.kind === "save"
                  ? `<button class="action-button is-danger"
                      data-session-detail-action="delete-independent-save"
                      data-filename="${escapeHTML(item.filename)}">删除文件</button>`
                  : ""
              }</div>`,
            )
            .join("") ||
          '<div class="empty-state compact"><span>尚未生成独立安全文件</span></div>'
        }
        </div>
      </div>
      <div class="section-toolbar">
        <p>上方独立 ZIP 与下方回合恢复点分别管理；最终保护 ZIP 会拒绝删除。</p>
        <div class="toolbar-actions">
          <button class="button button-primary" data-session-detail-action="new-save" ${
            readonly ? "disabled" : ""
          }>＋ 创建存档</button>
          <button class="button is-danger" data-session-detail-action="delete-session" ${
            ["closed", "finished"].includes(session.state) ? "" : "disabled"
          }>删除整个故事副本</button>
        </div>
      </div>
      <div class="tab-card" style="margin-top:14px">
        <div class="tab-card-head"><span class="row-icon" aria-hidden="true">💾</span><strong>回合恢复点</strong></div>
        <div class="session-stack">
        ${
          detail.snapshots.length
            ? detail.snapshots
                .map(
                  (snapshot) => `
                    <div class="session-row">
                      <span class="row-icon" aria-hidden="true">💾</span>
                      <div>
                        <div class="session-name">${escapeHTML(snapshot.name)}</div>
                        <div class="session-meta">第 ${escapeHTML(
                          snapshot.turn_no,
                        )} 回合 · ${escapeHTML(
                          snapshotKindLabel(snapshot.kind),
                        )} · ${escapeHTML(formatDate(snapshot.created_at))}</div>
                      </div>
                      <div class="session-location">${escapeHTML(
                        snapshot.created_by || "system",
                      )}</div>
                      <div class="table-actions">
                        <button class="action-button" data-session-detail-action="restore-save"
                          data-id="${escapeHTML(snapshot.id)}">恢复</button>
                        ${
                          ["safety", "undo"].includes(snapshot.kind)
                            ? ""
                            : `<button class="action-button is-danger"
                              data-session-detail-action="delete-save"
                              data-id="${escapeHTML(snapshot.id)}">删除</button>`
                        }
                      </div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前没有存档</span></div>'
        }
        </div>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="events">
      <div class="timeline">
        ${
          detail.events.length
            ? [...detail.events]
                .reverse()
                .map(
                  (event) => `
                    <div class="timeline-item role-${escapeHTML(event.role)}">
                      <div class="timeline-title">
                        <strong>${
                          {
                            narrator: "📖",
                            system: "⚙️",
                            dm: "🎮",
                            player: "🎭",
                            vote: "🗳",
                            check: "⚄",
                            timer: "⏱",
                          }[event.role] || "📄"
                        } ${escapeHTML(event.actor_name || event.role)}</strong>
                        <span>回合 ${escapeHTML(event.turn_no)} · ${escapeHTML(
                          formatDate(event.created_at),
                        )}</span>
                      </div>
                      <div class="timeline-content">${escapeHTML(event.content)}</div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>时间线尚无事件</span></div>'
        }
      </div>
    </div>
  `;
  Object.entries(SESSION_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, timing[key]),
  );
  if (readonly) {
    $$(
      "#session-modal-body [data-session-detail-action], #session-modal-body [data-timer-action]",
    ).forEach((item) => {
      if (
        ["delete-session", "delete-independent-save"].includes(
          item.dataset.sessionDetailAction,
        )
      ) {
        return;
      }
      item.disabled = true;
      item.title = "永久归档副本为只读；请从存档克隆新副本后继续";
    });
    $$(
      "#session-modal-body textarea, #session-modal-body input, #session-modal-body select",
    ).forEach((item) => {
      item.disabled = true;
    });
  }
  if (!$("#session-modal").open) {
    $("#session-modal").showModal();
  }
}

function openPlayerEditor(sessionId, player = null) {
  const item =
    player ||
    {
      session_id: sessionId,
      user_id: "",
      display_name: "",
      character_name: "",
      profile: {},
      enabled: true,
    };
  openEditor({
    title: player ? `编辑玩家「${player.display_name}」` : "添加玩家",
    kicker: "PLAYER BINDING",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="player-user-id">真实平台用户 ID</label>
          <input id="player-user-id" value="${escapeHTML(item.user_id)}" ${
            player ? "readonly" : ""
          } />
        </div>
        <div class="field">
          <label for="player-display-name">显示名称</label>
          <input id="player-display-name" value="${escapeHTML(
            item.display_name,
          )}" />
        </div>
        <div class="field field-span-2">
          <label for="player-character-name">剧中角色名</label>
          <input id="player-character-name" value="${escapeHTML(
            item.character_name,
          )}" placeholder="留空则沿用群昵称" />
        </div>
        <label class="switch-field field-span-2">
          <input id="player-enabled" type="checkbox" ${
            item.enabled ? "checked" : ""
          } />
          <span><strong>允许参与剧情</strong><small>停用后其发言不会进入模型</small></span>
        </label>
        <div class="field field-span-2">
          <label for="player-profile">角色能力与背景 JSON</label>
          <textarea id="player-profile" class="code-field">${escapeHTML(
            prettyJSON(item.profile),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("players/save", {
        session_id: sessionId,
        user_id: $("#player-user-id").value,
        display_name: $("#player-display-name").value,
        character_name: $("#player-character-name").value,
        enabled: $("#player-enabled").checked,
        profile: parseJSONField("#player-profile", "玩家资料"),
      });
      toast("玩家资料已保存", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    },
  });
}

function openSnapshotEditor(sessionId) {
  openEditor({
    title: "创建命名存档",
    kicker: "TIMELINE SNAPSHOT",
    body: `
      <div class="field">
        <label for="snapshot-name">存档名称</label>
        <input id="snapshot-name" maxlength="100" placeholder="例如：进入旧塔之前" />
        <small>发现同名存档时会展示覆盖确认，不再返回通用错误。</small>
      </div>
    `,
    saveLabel: "创建存档",
    onSave: async () => {
      const name = $("#snapshot-name").value.trim();
      const existing = (app.currentSession?.snapshots || []).find(
        (item) => item.name === name,
      );
      let replace = false;
      if (existing) {
        replace = await confirmAction(
          `覆盖同名存档「${name}」？`,
          `原存档位于第 ${existing.turn_no} 回合，覆盖后无法恢复旧内容。`,
          "确认覆盖",
        );
        if (!replace) return;
      }
      await bridge.apiPost("snapshots/create", {
        session_id: sessionId,
        name,
        replace,
      });
      toast(replace ? "同名存档已覆盖" : "存档已创建", "success");
      await openSessionDetail(sessionId);
    },
  });
}

async function loadMemories() {
  const sessionId = $("#memory-session-select").value;
  if (!sessionId) {
    app.memories = [];
    renderMemories();
    return;
  }
  const result = await bridge.apiGet("memories", {
    session_id: sessionId,
    q: $("#memory-search").value,
    limit: 200,
  });
  app.memories = result.items || [];
  renderMemories();
}

function renderMemories() {
  const root = $("#memory-grid");
  if (!$("#memory-session-select").value) {
    root.innerHTML =
      '<div class="empty-state"><div class="empty-symbol">≋</div><span>请先选择一个群会话</span></div>';
    return;
  }
  if (!app.memories.length) {
    root.innerHTML =
      '<div class="empty-state"><div class="empty-symbol">≋</div><strong>没有匹配的记忆</strong><span>可手工添加，或由叙事引擎在剧情中自动提取。</span></div>';
    return;
  }
  root.innerHTML = app.memories
    .map(
      (memory) => `
        <article class="memory-row">
          <div class="memory-importance" title="重要度 ${escapeHTML(
            memory.importance,
          )}">
            <span>${escapeHTML(memory.importance)}</span>
            <small>/ 5</small>
          </div>
          <div class="memory-main">
            <div class="memory-row-head">
              <span class="memory-kind">${escapeHTML(
                memory.scope,
              )} · ${escapeHTML(memory.kind)}</span>
              <span class="memory-scope-id">${escapeHTML(
                memory.scope_id || "全局",
              )}</span>
            </div>
            <div class="memory-content">${escapeHTML(memory.content)}</div>
            <div class="tag-row">
              ${(memory.tags || [])
                .map((tag) => `<span class="tag">${escapeHTML(tag)}</span>`)
                .join("")}
            </div>
          </div>
          <div class="memory-row-side">
            <time>${escapeHTML(formatDate(memory.updated_at))}</time>
            <div class="table-actions">
              <button class="action-button" data-memory-action="edit"
                data-id="${escapeHTML(memory.id)}">编辑</button>
              <button class="action-button is-danger" data-memory-action="delete"
                data-id="${escapeHTML(memory.id)}">删除</button>
            </div>
          </div>
        </article>
      `,
    )
    .join("");
}

function openSessionNPCEditor(sessionId, npc = null) {
  const item =
    npc ||
    {
      session_id: sessionId,
      name: "",
      aliases: [],
      role_type: "npc",
      public_profile: {},
      known_facts: [],
      misconceptions: [],
      state: {},
      review_status: "approved",
      lifecycle_status: "active",
      persistent: true,
    };
  openEditor({
    title: npc ? `编辑 NPC「${npc.name}」` : "添加副本 NPC",
    kicker: "SESSION CHARACTER",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="npc-name">名称</label>
          <input id="npc-name" value="${escapeHTML(item.name)}" maxlength="80" />
        </div>
        <div class="field">
          <label for="npc-role-type">角色类型</label>
          <input id="npc-role-type" value="${escapeHTML(item.role_type)}" maxlength="40" />
        </div>
        <div class="field field-span-2">
          <label for="npc-aliases">别名</label>
          <input id="npc-aliases" value="${escapeHTML(
            (item.aliases || []).join(", "),
          )}" placeholder="以逗号分隔；会检查重名与别名冲突" />
        </div>
        <div class="field">
          <label for="npc-review-status">复核状态</label>
          <select id="npc-review-status">
            ${[
              ["pending", "待复核"],
              ["approved", "已通过"],
              ["rejected", "已拒绝"],
              ["duplicate", "疑似重复"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.review_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="npc-lifecycle-status">剧情状态</label>
          <select id="npc-lifecycle-status">
            ${[
              ["active", "活跃"],
              ["departed", "离场"],
              ["dead", "死亡"],
              ["archived", "归档"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.lifecycle_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <label class="switch-field field-span-2">
          <input id="npc-persistent" type="checkbox" ${
            item.persistent ? "checked" : ""
          } />
          <span><strong>作为持久 NPC</strong><small>临时路人可以取消；只有相关 NPC 会进入模型上下文</small></span>
        </label>
        <div class="field field-span-2">
          <label for="npc-profile">公开资料 JSON</label>
          <textarea id="npc-profile" class="code-field">${escapeHTML(
            prettyJSON(item.public_profile),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-state">当前地点、阵营、关系与运行状态 JSON</label>
          <textarea id="npc-state" class="code-field">${escapeHTML(
            prettyJSON(item.state),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-known-facts">NPC 真正知道的事实</label>
          <textarea id="npc-known-facts" rows="4">${escapeHTML(
            (item.known_facts || []).join("\n"),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-misconceptions">误解、谣言与错误认知</label>
          <textarea id="npc-misconceptions" rows="4">${escapeHTML(
            (item.misconceptions || []).join("\n"),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("sessions/npc", {
        id: item.id,
        session_id: sessionId,
        revision: item.revision,
        name: $("#npc-name").value,
        aliases: splitLines($("#npc-aliases").value),
        role_type: $("#npc-role-type").value,
        public_profile: parseJSONField("#npc-profile", "NPC 公开资料"),
        state: parseJSONField("#npc-state", "NPC 运行状态"),
        known_facts: splitLines($("#npc-known-facts").value),
        misconceptions: splitLines($("#npc-misconceptions").value),
        review_status: $("#npc-review-status").value,
        lifecycle_status: $("#npc-lifecycle-status").value,
        persistent: $("#npc-persistent").checked,
      });
      toast("副本 NPC 已保存", "success");
      await loadCore();
    },
  });
}

function openMemoryEditor(memory = null, requestedSessionId = "") {
  const sessionId =
    requestedSessionId ||
    memory?.session_id ||
    $("#memory-session-select").value;
  if (!sessionId) {
    toast("请先选择会话", "error");
    return;
  }
  const item =
    memory ||
    {
      session_id: sessionId,
      scope: "world",
      scope_id: "",
      kind: "fact",
      content: "",
      importance: 3,
      tags: [],
      visibility: "public",
      locked: false,
      pinned: false,
      invalidated: false,
      supersedes_id: "",
      conflict_status: "clear",
      governance_note: "",
    };
  openEditor({
    title: memory ? "编辑长期记忆" : "添加长期记忆",
    kicker: "MEMORY RECORD",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="memory-scope">范围</label>
          <select id="memory-scope">
            ${["world", "player", "npc"]
              .map(
                (scope) =>
                  `<option value="${scope}" ${
                    item.scope === scope ? "selected" : ""
                  }>${scope}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="memory-scope-id">范围对象 ID</label>
          <input id="memory-scope-id" value="${escapeHTML(item.scope_id)}" />
        </div>
        <div class="field">
          <label for="memory-kind">类型</label>
          <input id="memory-kind" value="${escapeHTML(item.kind)}" />
        </div>
        <div class="field">
          <label for="memory-importance">重要度（1-5）</label>
          <input id="memory-importance" type="number" min="1" max="5"
            value="${escapeHTML(item.importance)}" />
        </div>
        <div class="field">
          <label for="memory-visibility">可见范围</label>
          <select id="memory-visibility">
            ${[
              ["public", "全队公开"],
              ["host", "仅主持人"],
              ["private", "个人私密"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.visibility === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="memory-conflict-status">事实状态</label>
          <select id="memory-conflict-status">
            ${[
              ["clear", "明确"],
              ["conflict", "冲突待确认"],
              ["resolved", "冲突已解决"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.conflict_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field field-span-2">
          <label for="memory-content-field">记忆事实</label>
          <textarea id="memory-content-field" rows="6">${escapeHTML(
            item.content,
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="memory-tags">标签</label>
          <input id="memory-tags" value="${escapeHTML(
            (item.tags || []).join(", "),
          )}" placeholder="以逗号分隔" />
        </div>
        <div class="field">
          <label for="memory-supersedes">替代的旧记忆 ID</label>
          <input id="memory-supersedes" value="${escapeHTML(
            item.supersedes_id || "",
          )}" placeholder="可留空" />
        </div>
        <div class="field">
          <label for="memory-governance-note">治理备注</label>
          <input id="memory-governance-note" value="${escapeHTML(
            item.governance_note || "",
          )}" placeholder="例如：经主持人确认" />
        </div>
        <label class="switch-field">
          <input id="memory-locked" type="checkbox" ${
            item.locked ? "checked" : ""
          } />
          <span><strong>锁定</strong><small>重要事实不会被上下文预算裁掉</small></span>
        </label>
        <label class="switch-field">
          <input id="memory-pinned" type="checkbox" ${
            item.pinned ? "checked" : ""
          } />
          <span><strong>置顶</strong><small>优先进入近期叙事上下文</small></span>
        </label>
        <label class="switch-field field-span-2">
          <input id="memory-invalidated" type="checkbox" ${
            item.invalidated ? "checked" : ""
          } />
          <span><strong>标记失效</strong><small>保留审计，但不再作为有效世界事实</small></span>
        </label>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("memories/save", {
        id: item.id,
        session_id: sessionId,
        scope: $("#memory-scope").value,
        scope_id: $("#memory-scope-id").value,
        kind: $("#memory-kind").value,
        content: $("#memory-content-field").value,
        importance: Number($("#memory-importance").value),
        tags: splitLines($("#memory-tags").value),
        visibility: $("#memory-visibility").value,
        locked: $("#memory-locked").checked,
        pinned: $("#memory-pinned").checked,
        invalidated: $("#memory-invalidated").checked,
        supersedes_id: $("#memory-supersedes").value,
        conflict_status: $("#memory-conflict-status").value,
        governance_note: $("#memory-governance-note").value,
      });
      toast("长期记忆已保存", "success");
      if ($("#memory-session-select").value === sessionId) {
        await loadMemories();
      }
      await loadCore();
    },
  });
}

async function loadAudit() {
  const result = await bridge.apiGet("audit", {
    session_id: $("#audit-session-select").value,
    limit: 200,
    offset: 0,
  });
  app.audit = result.items || [];
  renderAudit();
}

function renderAudit() {
  const body = $("#audit-table-body");
  if (!app.audit.length) {
    body.innerHTML =
      '<tr><td colspan="5"><div class="empty-state compact"><span>暂无审计记录</span></div></td></tr>';
    return;
  }
  body.innerHTML = app.audit
    .map((item) => {
      const detail = prettyJSON(item.detail);
      const shortDetail = detail.length > 150 ? `${detail.slice(0, 150)}…` : detail;
      return `
        <tr>
          <td>${escapeHTML(formatDate(item.created_at))}</td>
          <td><span class="table-title">${escapeHTML(item.action)}</span></td>
          <td>${escapeHTML(item.actor_id || "system")}</td>
          <td>${escapeHTML(item.target || "—")}</td>
          <td><code title="${escapeHTML(detail)}">${escapeHTML(shortDetail)}</code></td>
        </tr>
      `;
    })
    .join("");
}

function renderSettings() {
  const s = app.settings;
  if (!s) return;
  const configState = app.configState || {};
  $("#settings-revision").textContent = configState.revision
    ? `配置修订 r${configState.revision} · ${formatDate(configState.saved_at)}`
    : "配置修订：尚未记录";
  $("#provider-health-list").innerHTML = app.providerHealth.length
    ? app.providerHealth
        .map(
          (item) => `
            <div class="provider-health-item status-${escapeHTML(item.status)}">
              <strong>${escapeHTML(item.provider_id)}</strong>
              <span>${escapeHTML(
                item.status === "open"
                  ? `熔断至 ${formatDate(item.circuit_until)}`
                  : item.status === "half_open"
                    ? "等待试探请求"
                    : "健康",
              )}</span>
              <small>${escapeHTML(
                item.last_failure_reason ||
                  `连续失败 ${item.consecutive_failures || 0} 次`,
              )}</small>
            </div>
          `,
        )
        .join("")
    : '<div class="empty-inline">模型链尚无失败记录。</div>';
  $("#setting-admin-ids").value = (s.security.admin_ids || []).join("\n");
  $("#setting-group-ids").value = (s.security.allowed_group_ids || []).join("\n");
  $("#setting-require-whitelist").checked = s.security.require_group_whitelist;
  $("#setting-public-status").checked = s.security.public_status;
  $("#setting-unauthorized").value = s.security.unauthorized_command_behavior;

  $("#setting-provider").innerHTML = providerOptionHTML(
    s.model.provider_id || "",
    "跟随当前群会话模型",
  );
  $("#setting-image-provider").innerHTML = providerOptionHTML(
    s.model.image_caption_provider_id || "",
    "未配置（拒绝带图行动）",
  );
  renderFallbackProviders(s.model.fallback_provider_ids || []);
  $("#setting-image-prompt").value = s.model.image_caption_prompt || "";
  $("#setting-max-images").value = s.model.max_images_per_turn || 4;
  $("#setting-temperature").value = s.model.temperature;
  $("#setting-max-tokens").value = s.model.max_tokens;
  $("#setting-timeout").value = s.model.request_timeout_seconds;
  $("#setting-repair").value = s.model.json_repair_attempts;
  $("#setting-two-phase").checked = s.runtime.two_phase_checks;

  $("#setting-trigger-prefix").value = s.runtime.trigger_prefix || "jg";
  $("#setting-command-triggers").value = (s.runtime.command_triggers || ["酒馆"]).join("\n");
  $("#setting-default-world").value = s.runtime.default_world_slug;
  $("#setting-card-review-notification-mode").value =
    s.runtime.card_review_notification_mode || "both";
  $("#setting-recent-turns").value = s.runtime.recent_turns;
  $("#setting-memory-limit").value = s.runtime.memory_limit;
  $("#setting-snapshot-interval").value = s.runtime.auto_snapshot_interval;
  $("#setting-cooldown").value = s.runtime.user_cooldown_seconds;
  $("#setting-input-limit").value = s.runtime.max_input_chars;
  $("#setting-output-limit").value = s.runtime.max_output_chars;
  $("#setting-ooc-prefixes").value = (s.runtime.ooc_prefixes || []).join(", ");
  const timeRules = s.runtime.time_rules || {};
  Object.entries(GLOBAL_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, timeRules[key]),
  );
  $("#gtime-timeout-count").value =
    timeRules.max_consecutive_timeouts ?? 2;
  $("#gtime-pause-clock").checked = timeRules.pause_stops_clock !== false;
  $("#gtime-announce").checked = timeRules.announce_timeouts !== false;
  $("#gtime-turn-action").value = timeRules.turn_timeout_action || "skip";
  $("#gtime-card-action").value = timeRules.card_timeout_action || "standby";
  $("#gtime-ready-action").value = timeRules.ready_timeout_action || "standby";

  $("#setting-audit-days").value = s.advanced.audit_retention_days;
  $("#setting-model-payloads").checked = s.advanced.store_model_payloads;
  $("#setting-debug").checked = s.advanced.debug;
}

function collectSettings() {
  return {
    security: {
      admin_ids: splitLines($("#setting-admin-ids").value),
      allowed_group_ids: splitLines($("#setting-group-ids").value),
      require_group_whitelist: $("#setting-require-whitelist").checked,
      unauthorized_command_behavior: $("#setting-unauthorized").value,
      public_status: $("#setting-public-status").checked,
    },
    model: {
      provider_id: $("#setting-provider").value,
      fallback_provider_ids: [
        ...$("#fallback-provider-list").querySelectorAll(
          "[data-fallback-provider]",
        ),
      ]
        .map((item) => item.value)
        .filter(
          (item, index, list) =>
            item &&
            item !== $("#setting-provider").value &&
            list.indexOf(item) === index,
        ),
      image_caption_provider_id: $("#setting-image-provider").value,
      image_caption_prompt: $("#setting-image-prompt").value.trim(),
      max_images_per_turn: Number($("#setting-max-images").value),
      temperature: Number($("#setting-temperature").value),
      max_tokens: Number($("#setting-max-tokens").value),
      request_timeout_seconds: Number($("#setting-timeout").value),
      json_repair_attempts: Number($("#setting-repair").value),
    },
    runtime: {
      default_world_slug: $("#setting-default-world").value.trim(),
      trigger_prefix: $("#setting-trigger-prefix").value.trim(),
      command_triggers: $("#setting-command-triggers")
        .value.split(/[\n,，]+/)
        .map((item) => item.trim())
        .filter(Boolean),
      two_phase_checks: $("#setting-two-phase").checked,
      max_input_chars: Number($("#setting-input-limit").value),
      max_output_chars: Number($("#setting-output-limit").value),
      recent_turns: Number($("#setting-recent-turns").value),
      memory_limit: Number($("#setting-memory-limit").value),
      user_cooldown_seconds: Number($("#setting-cooldown").value),
      auto_snapshot_interval: Number($("#setting-snapshot-interval").value),
      card_review_notification_mode: $("#setting-card-review-notification-mode").value,
      ooc_prefixes: splitLines($("#setting-ooc-prefixes").value),
      time_rules: {
        ...(app.settings?.runtime?.time_rules || {}),
        card_code_ttl_seconds: readTimeValue("#gtime-card-code"),
        card_draft_ttl_seconds: readTimeValue("#gtime-card-draft"),
        card_completion_timeout_seconds: readTimeValue("#gtime-card-completion"),
        preparation_timeout_seconds: readTimeValue("#gtime-preparation"),
        ready_timeout_seconds: readTimeValue("#gtime-ready"),
        turn_timeout_seconds: readTimeValue("#gtime-turn"),
        turn_reminder_seconds: readTimeValue("#gtime-turn-reminder"),
        max_consecutive_timeouts: Number($("#gtime-timeout-count").value),
        standby_timeout_seconds: readTimeValue("#gtime-standby"),
        delegation_ttl_seconds: readTimeValue("#gtime-delegation"),
        all_idle_pause_seconds: readTimeValue("#gtime-all-idle"),
        vote_round_one_seconds: readTimeValue("#gtime-vote-one"),
        vote_round_two_seconds: readTimeValue("#gtime-vote-two"),
        vote_reminder_seconds: readTimeValue("#gtime-vote-reminder"),
        pause_stops_clock: $("#gtime-pause-clock").checked,
        announce_timeouts: $("#gtime-announce").checked,
        turn_timeout_action: $("#gtime-turn-action").value,
        card_timeout_action: $("#gtime-card-action").value,
        ready_timeout_action: $("#gtime-ready-action").value,
      },
    },
    advanced: {
      audit_retention_days: Number($("#setting-audit-days").value),
      store_model_payloads: $("#setting-model-payloads").checked,
      debug: $("#setting-debug").checked,
    },
  };
}

function confirmAction(title, message, confirmLabel = "确认") {
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-submit").textContent = confirmLabel;
  const modal = $("#confirm-modal");
  modal.showModal();
  return new Promise((resolve) => {
    const handler = () => {
      modal.removeEventListener("close", handler);
      resolve(modal.returnValue === "confirm");
    };
    modal.addEventListener("close", handler);
  });
}

async function handleWorldAction(button) {
  const world = app.worlds.find((item) => item.id === button.dataset.id);
  if (!world) return;
  const action = button.dataset.worldAction;
  if (action === "edit") openWorldEditor(world);
  if (action === "characters") await openCharacterManager(world.id);
  if (action === "card-template") openCharacterCardTemplateManager(world);
  if (action === "preflight") {
    const response = await bridge.apiPost("worlds/preflight", { world_ref: world.id });
    const report = response.report || {};
    const summary = report.summary || {};
    openEditor({
      title: `${world.name} · 世界包体检`,
      kicker: report.compatible ? "COMPATIBLE" : "BLOCKED",
      body: `
        <div class="detail-grid">
          <div class="detail-card"><span>协议</span><strong>v${escapeHTML(summary.schema_version || "?")}</strong></div>
          <div class="detail-card"><span>数值</span><strong>${escapeHTML(summary.stats_mode || "unknown")}</strong></div>
          <div class="detail-card"><span>裁定</span><strong>${escapeHTML(summary.resolution_mode || "unknown")}</strong></div>
          <div class="detail-card"><span>结果</span><strong>${report.compatible ? "可导入" : "需修复"}</strong></div>
          <div class="detail-card"><span>注册实体</span><strong>${escapeHTML(summary.entity_count || 0)}</strong></div>
          <div class="detail-card"><span>功能模块</span><strong>${escapeHTML(Object.keys(summary.feature_versions || {}).length)}</strong></div>
        </div>
        <div class="session-stack" style="margin-top:18px">
          ${(report.issues || []).map((item) => `<div class="session-row"><div><div class="session-name">${escapeHTML(item.level.toUpperCase())} · ${escapeHTML(item.message)}</div><div class="session-meta">${escapeHTML(item.path)} · ${escapeHTML(item.code)}</div></div></div>`).join("") || '<div class="empty-state compact"><span>未发现兼容性问题</span></div>'}
        </div>
        <h3 style="margin-top:22px">试运行</h3>
        <div class="command-list">${(report.tests || []).map((item) => `<code>${item.status === "passed" ? "✓" : "×"} ${escapeHTML(item.name)}</code>`).join("")}</div>
      `,
    });
  }
  if (action === "simulate") openRuleSimulator(world);
  if (action === "up" || action === "down") {
    const index = app.worlds.findIndex((item) => item.id === world.id);
    const neighbor = app.worlds[index + (action === "up" ? -1 : 1)];
    if (!neighbor) return;
    const currentOrder = Number(world.sort_order || world.display_no || index + 1);
    const neighborOrder = Number(neighbor.sort_order || neighbor.display_no || index + 2);
    await bridge.apiPost("worlds/order", { id: world.id, sort_order: neighborOrder });
    await bridge.apiPost("worlds/order", { id: neighbor.id, sort_order: currentOrder });
    await loadCore();
  }
  if (action === "archive") {
    const ok = await confirmAction(
      `归档「${world.name}」？`,
      "归档后不能用于新会话；正在运行的会话会阻止此操作。已有数据不会删除。",
      "归档世界",
    );
    if (!ok) return;
    await bridge.apiPost("worlds/archive", { id: world.id });
    toast("世界包已归档", "success");
    await loadCore();
  }
  if (action === "restore") {
    await bridge.apiPost("worlds/restore", { id: world.id });
    toast("世界包已恢复", "success");
    await loadCore();
  }
}

function openRuleSimulator(world) {
  const definitions = world.rules?.capabilities?.definitions || [];
  const first = Array.isArray(definitions) ? definitions[0] : null;
  const capabilityRef = first ? `capability:${first.capability_id || first.id}` : "";
  openEditor({
    title: `${world.name} · 规则模拟器`,
    kicker: "DRY RUN · 不修改存档",
    body: `
      <div class="field"><label for="rule-sim-intent">行动意图 JSON</label>
        <textarea id="rule-sim-intent" class="code-field" rows="10">${escapeHTML(prettyJSON({actor_ref: "character:preview", action_type: "freeform", capability_ref: capabilityRef, targets: [], parameters: {}, declared_intent: "预览规则执行"}))}</textarea></div>
      <div class="field"><label for="rule-sim-context">受控上下文 JSON</label>
        <textarea id="rule-sim-context" class="code-field" rows="12">${escapeHTML(prettyJSON({actor: {refs: {}, capabilities: capabilityRef ? [{capability_ref: capabilityRef, available: true}] : []}, target: {refs: {}}, scene: {refs: {}}, state: {refs: {}, tags: [], references: []}}))}</textarea></div>
      <button type="button" class="button button-primary" id="run-rule-simulation">执行只读模拟</button>
      <div class="template-preview" id="rule-sim-result"><span>结果会逐步显示读取值、命中规则、裁定步骤与拟提交操作。</span></div>
    `,
  });
  $("#run-rule-simulation").addEventListener("click", async () => {
    try {
      const response = await bridge.apiPost("worlds/simulate", {
        world_ref: world.id,
        intent: parseJSONField("#rule-sim-intent", "行动意图"),
        context: parseJSONField("#rule-sim-context", "模拟上下文"),
      });
      $("#rule-sim-result").innerHTML = `<pre>${escapeHTML(prettyJSON(response.result || {}))}</pre>`;
      toast("规则模拟完成，未修改任何存档", "success");
    } catch (error) {
      showError(error);
    }
  });
}

function openGroupRemarkEditor(button) {
  const platformId = button.dataset.platformId;
  const groupId = button.dataset.groupId;
  const currentRemark = button.dataset.groupRemark || "";
  const revision = Number(button.dataset.groupRevision || 1);
  openEditor({
    title: currentRemark ? `编辑群备注「${currentRemark}」` : "添加群备注",
    kicker: "GROUP NOTE",
    body: `
      <div class="field">
        <label for="group-remark-input">群备注</label>
        <input id="group-remark-input" maxlength="120"
          value="${escapeHTML(currentRemark)}"
          placeholder="例如：周六固定团、测试一群" />
        <small>群备注只用于管理台显示和检索，不会改变稳定目录名或群 ID。</small>
      </div>
      <div class="binding-guide">
        <strong>${escapeHTML(platformId)}</strong>
        <span>群 ID：${escapeHTML(groupId)}</span>
      </div>
    `,
    saveLabel: currentRemark ? "保存备注" : "添加备注",
    onSave: async () => {
      await bridge.apiPost("groups/remark", {
        platform_id: platformId,
        group_id: groupId,
        remark: $("#group-remark-input").value.trim(),
        revision,
      });
      toast("群备注已保存", "success");
      await loadSessionPage();
    },
  });
}

async function openGroupTokenQuotaEditor(button) {
  const platformId = button.dataset.platformId;
  const groupId = button.dataset.groupId;
  const groupName = button.dataset.groupName || `群 ${groupId}`;
  const response = await bridge.apiGet("groups/token-usage", {
    platform_id: platformId,
    group_id: groupId,
  });
  const usage = response.usage || {};
  const totals = usage.group || { hour: 0, day: 0, all: 0 };
  const quota = usage.quota || {};
  openEditor({
    title: `${groupName} · Token 限额`,
    kicker: "GROUP TOKEN BUDGET",
    body: `
      <div class="detail-grid">
        <div class="detail-card"><span>群 1 小时</span><strong>${escapeHTML(
          totals.hour || 0,
        )}</strong></div>
        <div class="detail-card"><span>群 24 小时</span><strong>${escapeHTML(
          totals.day || 0,
        )}</strong></div>
        <div class="detail-card"><span>群累计</span><strong>${escapeHTML(
          totals.all || 0,
        )}</strong></div>
      </div>
      <div class="form-grid" style="margin-top:18px">
        <label class="switch-field"><input id="group-quota-enabled" type="checkbox" ${
          quota.enabled ? "checked" : ""
        } /><span><strong>启用群 Token 限额</strong><small>该群内全部故事副本共享同一额度</small></span></label>
        <div class="field"><label for="group-quota-window">群滚动窗口（秒）</label>
          <input id="group-quota-window" type="number" min="60"
            value="${escapeHTML(quota.window_seconds || 86400)}" /></div>
        <div class="field"><label for="group-quota-limit">群 Token 上限</label>
          <input id="group-quota-limit" type="number" min="1"
            value="${escapeHTML(quota.token_limit || 500000)}" /></div>
      </div>
      <div class="binding-guide">
        <strong>${escapeHTML(platformId)}</strong>
        <span>群 ID：${escapeHTML(groupId)}</span>
      </div>
    `,
    saveLabel: "保存群限额",
    onSave: async () => {
      const windowSeconds = Number($("#group-quota-window").value);
      const tokenLimit = Number($("#group-quota-limit").value);
      if (
        !Number.isInteger(windowSeconds) ||
        windowSeconds < 60 ||
        !Number.isInteger(tokenLimit) ||
        tokenLimit < 1
      ) {
        throw new Error("Token 限额窗口至少 60 秒，Token 上限必须为正整数");
      }
      await bridge.apiPost("groups/token-quota", {
        platform_id: platformId,
        group_id: groupId,
        enabled: $("#group-quota-enabled").checked,
        window_seconds: windowSeconds,
        token_limit: tokenLimit,
      });
      toast("群 Token 限额已保存", "success");
      await loadSessionPage();
    },
  });
}

async function handleCharacterAction(button) {
  const action = button.dataset.characterAction;
  const worldId = button.dataset.worldId;
  if (action === "new") {
    openCharacterEditor(worldId);
    return;
  }
  const character = (app.currentCharacters || []).find(
    (item) => item.id === button.dataset.id,
  );
  if (!character) return;
  if (action === "edit") {
    openCharacterEditor(worldId, character);
  } else if (action === "delete") {
    const ok = await confirmAction(
      `删除角色「${character.name}」？`,
      "角色卡会从世界包中永久移除。群会话的历史记录不会被改写。",
      "删除角色",
    );
    if (!ok) return;
    await bridge.apiPost("characters/delete", { id: character.id });
    toast("角色已删除", "success");
    await loadCore();
    await openCharacterManager(worldId);
  }
}

async function handleSessionDetailAction(button) {
  // A18: SESSION INSPECTOR 与 LIVE 仪表盘共用同一处理函数。
  // 之前只读 app.currentSession（仅 SESSION INSPECTOR 设置），
  // 从 LIVE 打开时为空导致所有 DM/托管按钮静默失效；现回退到 LIVE 上下文。
  const ctx = resolveLiveDetailContext();
  if (!ctx) {
    toast("未选中副本，无法执行该操作", "error");
    return;
  }
  const detail = ctx.detail;
  const sessionId = ctx.sessionId;
  const action = button.dataset.sessionDetailAction;
  if (action === "dm-enable") {
    const dmUserId = await promptForText({
      title: "开启 / 接管主持",
      kicker: "DM CONTROL",
      label: "活动 DM 的 QQ 用户 ID",
      defaultValue: detail.control?.active_dm_user_id || "",
      required: true,
    });
    if (!dmUserId) return;
    await bridge.apiPost("sessions/action", { session_id: sessionId, action: "dm_enable", dm_user_id: dmUserId });
    toast("主持模式已开启", "success");
    await openSessionDetail(sessionId);
  } else if (action === "dm-directive") {
    const directive = await promptForText({
      title: "一次性导演指引",
      kicker: "DM CONTROL",
      label: "下一次 AI 主持推进的一次性指引",
      defaultValue: detail.control?.directive || "",
      multiline: true,
    });
    if (!directive) return;
    await bridge.apiPost("sessions/action", { session_id: sessionId, action: "dm_directive", dm_user_id: detail.control?.active_dm_user_id, directive });
    toast("一次性指引已保存", "success");
    await openSessionDetail(sessionId);
  } else if (action === "dm-direct") {
    const narrative = await promptForText({
      title: "原文插入正式剧情",
      kicker: "DM CONTROL",
      label: "按原文插入的剧情（不会自动修改机械状态）",
      multiline: true,
      required: true,
    });
    if (!narrative) return;
    await bridge.apiPost("sessions/action", { session_id: sessionId, action: "dm_direct", dm_user_id: detail.control?.active_dm_user_id, narrative });
    toast("主持原文已提交", "success");
    await openSessionDetail(sessionId);
  } else if (action === "dm-disable") {
    await bridge.apiPost("sessions/action", { session_id: sessionId, action: "dm_disable" });
    toast("已恢复 AI 自动模式", "success");
    await openSessionDetail(sessionId);
  } else if (action === "download-diagnostics") {
    await withBusy(button, async () => {
      await bridge.download(
        "sessions/diagnostics",
        { id: sessionId },
        `tavern_diagnostic_${sessionId}.zip`,
      );
    });
    toast("脱敏诊断包已生成", "success");
  } else if (action === "request-card-revision" || action === "edit-card") {
    const participant = (detail.roster || []).find((item) => item.id === button.dataset.ref);
    if (!participant) throw new Error("没有找到对应角色");
    $("#session-modal").close();
    openEditor({
      title: `${participant.character_name || participant.display_name} · 新建角色卡版本`,
      kicker: "CARD REVISION",
      body: `
        <p class="field-hint">修改会生成新版本并进入审核；审核通过前，当前副本继续使用旧版本。</p>
        <div class="field"><label for="card-revision-name">角色名（≤ 12 字）</label>
          <input id="card-revision-name" maxlength="12" value="${escapeHTML(
            participant.character_name || "",
          )}" /></div>
        <div class="field"><label for="card-revision-code">代号 / 昵称（≤ 12 字）</label>
          <input id="card-revision-code" maxlength="12" value="${escapeHTML(
            participant.character_code || "",
          )}" placeholder="例如：ALD-01 / 灰羽" /></div>
        <div class="field"><label for="card-revision-profile">完整角色资料 JSON</label>
          <textarea id="card-revision-profile" class="code-field" rows="16">${escapeHTML(prettyJSON(participant.card_profile || {}))}</textarea></div>
        <div class="field"><label for="card-revision-note">修改说明</label>
          <input id="card-revision-note" maxlength="500" placeholder="例如：修正背景错字与专长描述" /></div>
      `,
      saveLabel: "提交修改申请",
      onSave: async () => {
        const profile = parseJSONField("#card-revision-profile", "角色资料");
        const name = $("#card-revision-name")?.value.trim();
        if (name) profile.name = name;
        const code =
          ($("#card-revision-code")?.value ?? "").trim() ||
          participant.character_code ||
          "";
        if (code) profile.code = code;
        await requestCardRevision({
          sessionId,
          participantRef: participant.id,
          profilePatch: profile,
          note: $("#card-revision-note").value.trim(),
        });
        toast("角色卡新版本已提交审核", "success");
        await openSessionDetail(sessionId);
      },
    });
  } else if (action === "rename-card") {
    const participant = (detail.roster || []).find((item) => item.id === button.dataset.ref);
    if (!participant) throw new Error("没有找到对应角色");
    // A9：替换被 iframe 拦截的 window.prompt，使用控制台统一编辑弹窗。
    $("#session-modal").close();
    openEditor({
      title: `${participant.character_name || participant.display_name} · 修改角色信息`,
      kicker: "CARD RENAME",
      body: `
        <p class="field-hint">修改会生成新版本并进入审核；审核通过前，当前副本继续使用旧信息。角色名与「代号 / 昵称」（别名）会同步到世界模板字段并写入角色卡。</p>
        <div class="field"><label for="rename-name">角色名（≤ 12 字）</label>
          <input id="rename-name" maxlength="12" value="${escapeHTML(
            participant.character_name || "",
          )}" /></div>
        <div class="field"><label for="rename-code">代号 / 昵称（≤ 12 字）</label>
          <input id="rename-code" maxlength="12" value="${escapeHTML(
            participant.character_code || "",
          )}" placeholder="例如：ALD-01 / 灰羽" /></div>
        <div class="field"><label for="rename-note">修改说明</label>
          <input id="rename-note" maxlength="500" placeholder="例如：修正角色名与代号设定" /></div>`,
      saveLabel: "提交修改申请",
      onSave: async () => {
        const name = $("#rename-name")?.value.trim();
        const code =
          ($("#rename-code")?.value ?? "").trim() ||
          participant.character_code ||
          "";
        if (!name) throw new Error("角色名不能为空");
        if (
          name === participant.character_name &&
          code === participant.character_code
        ) {
          throw new Error("角色名与代号均未变化");
        }
        await requestCardRevision({
          sessionId,
          participantRef: participant.id,
          profilePatch: { name, code },
          note:
            $("#rename-note")?.value.trim() ||
            `WebUI 角色信息修改：${name} / ${code}`,
        });
        toast("角色名与代号修改已提交审核，可在「准备与角色」下方审核", "success");
        await openSessionDetail(sessionId);
      },
    });
  } else if (action === "cancel-operation") {
    const ok = await confirmAction("放弃未完成任务？", "该操作不会回滚已经提交的剧情，只会解除卡死的事务锁。", "放弃任务");
    if (!ok) return;
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "cancel_operation",
      operation_id: button.dataset.operationId,
      reason: "WebUI 管理员主动终止",
    });
    toast("未完成任务已标记为失败，可重新提交", "success");
    await openSessionDetail(sessionId);
  } else if (action === "replace-choices") {
    let choices;
    try {
      choices = JSON.parse($("#rescue-choices-json").value || "[]");
    } catch (error) {
      throw new Error(`选项不是有效 JSON：${error.message}`);
    }
    if (!Array.isArray(choices)) throw new Error("选项必须是 A—D 数组");
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "replace_choices",
      choices,
    });
    toast("当前 A—D 选项已替换", "success");
    await openSessionDetail(sessionId);
  } else if (["edit-last-narrative", "bridge-narrative"].includes(action)) {
    const narrative = $("#rescue-narrative").value.trim();
    if (!narrative) throw new Error("请先填写故事正文");
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: action === "edit-last-narrative" ? "edit_last_narrative" : "bridge_narrative",
      narrative,
    });
    toast(action === "edit-last-narrative" ? "上一段故事正文已修订" : "过渡剧情已插入", "success");
    await openSessionDetail(sessionId);
  } else if (action === "rollback-before-turn") {
    const ok = await confirmAction("回到本轮之前？", "将恢复最近一个自动、安全或单回合保护点，并暂停副本。", "确认回滚");
    if (!ok) return;
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "rollback_before_turn",
    });
    toast("已恢复到最近保护点", "success");
    await openSessionDetail(sessionId);
  } else if (["revision-approve", "revision-reject"].includes(action)) {
    await reviewCardRevision(
      button.dataset.requestId,
      action === "revision-approve",
      sessionId,
    );
  } else if (action === "force-ready") {
    const ok = await confirmAction(
      "强制所有合格角色准备？",
      "只处理角色卡已审核通过且当前出场的玩家；不会绕过建卡或审核，也不会自动开演。",
      "确认强制准备",
    );
    if (!ok) return;
    await withBusy(button, async () => {
      const response = await bridge.apiPost("sessions/action", {
        session_id: sessionId,
        action: "force_ready",
      });
      toast(
        `已强制准备 ${response.result?.ready_count || 0} 人`,
        "success",
      );
      await openSessionDetail(sessionId);
    });
  } else if (action === "save-session-token-quota") {
    const policy = {
      scope_type: "session",
      enabled: $("#quota-session-enabled").checked,
      window_seconds: Number($("#quota-session-window").value),
      token_limit: Number($("#quota-session-limit").value),
    };
    if (
      !Number.isInteger(policy.window_seconds) ||
      policy.window_seconds < 60 ||
      !Number.isInteger(policy.token_limit) ||
      policy.token_limit < 1
    ) {
      throw new Error("Token 限额窗口至少 60 秒，Token 上限必须为正整数");
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/token-quota", {
        session_id: sessionId,
        ...policy,
      });
      toast("副本 Token 限额已保存", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "token-danger") {
    const danger = button.dataset.danger || "reset";
    const ok = await confirmAction(
      "重置 Token 统计？",
      "仅清空当前副本的 Token 用量流水，不删除任何剧情与配置。",
      "确认重置",
    );
    if (!ok) return;
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/token-reset", { session_id: sessionId });
      toast("Token 统计已重置", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "econ-toggle") {
    await withBusy(button, async () => {
      const enabled = button.dataset.enabled === "1";
      await bridge.apiPost("economy/set-enabled", {
        session_id: sessionId,
        enabled: !enabled,
      });
      toast(!enabled ? "经济系统已启用" : "经济系统已关闭", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "econ-adjust") {
    await withBusy(button, async () => {
      await bridge.apiPost("economy/adjust", {
        session_id: sessionId,
        operation_id: `web:${Date.now()}:${Math.random().toString(16).slice(2, 8)}`,
        currency_id: button.dataset.currency,
        kind: button.dataset.kind || "adjust",
        amount: button.dataset.amount,
        owner_type: button.dataset.ownerType,
        owner_ref: button.dataset.ownerRef,
        reason: button.dataset.reason || "WebUI 调整",
      });
      toast("资产已调整", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "econ-tx") {
    const resp = await bridge.apiGet("economy/transactions", {
      session_id: sessionId,
      limit: 100,
    });
    openEditor({
      title: "经济交易日志",
      kicker: "ECONOMY · TRANSACTIONS",
      body: `<pre class="json-preview">${escapeHTML(JSON.stringify(resp.items || [], null, 2))}</pre>`,
    });
  } else if (action === "deleg-grant") {
    const owner = button.dataset.owner;
    const candidates = (detail.roster || []).filter(
      (p) =>
        p.group_user_id &&
        p.group_user_id !== owner &&
        ["active", "reserved"].includes(p.participation_status),
    );
    const options = candidates
      .map(
        (p) =>
          `<option value="${escapeHTML(p.group_user_id)}">${escapeHTML(
            p.character_name || p.display_name || p.group_user_id,
          )}（${escapeHTML(p.group_user_id)}）</option>`,
      )
      .join("");
    openEditor({
      title: `托管「${escapeHTML(owner)}」的角色`,
      kicker: "DELEGATION",
      body: `
        <p class="field-hint">选择控制该角色的玩家（真实平台用户 ID，阵容中已列出）；授权后代理人可代选/代投票/重整/跳过。</p>
        <div class="field">
          <label for="deleg-target">代理人（玩家 ID）</label>
          <select id="deleg-target">${options}</select>
        </div>
        <div class="field">
          <label for="deleg-expiry">托管期限</label>
          <select id="deleg-expiry">
            <option value="none">手动取消前持续</option>
            <option value="datetime">按时间到期（默认 2 小时）</option>
            <option value="round">至本回合结束</option>
            <option value="instance">至副本结束</option>
          </select>
        </div>
        <label class="switch-field"><input id="deleg-auto-restore" type="checkbox" /><span><strong>到期自动恢复原玩家</strong><small>到期后控制权自动交回</small></span></label>`,
      saveLabel: "授予托管",
      onSave: async () => {
        const target = $("#deleg-target")?.value;
        if (!target) throw new Error("请选择代理人");
        await bridge.apiPost("delegations/grant", {
          session_id: sessionId,
          owner_user_id: owner,
          delegate_user_id: target,
          source: "admin",
          permissions: ["choose", "vote", "reroll", "skip"],
          expiry_kind: $("#deleg-expiry")?.value || "none",
          auto_restore: Boolean($("#deleg-auto-restore")?.checked),
        });
        toast("已授予托管", "success");
        await refreshDetailViews(sessionId);
      },
    });
  } else if (action === "deleg-revoke") {
    const ok = await confirmAction("撤销该角色全部托管？", "恢复由原玩家本人控制。", "确认撤销");
    if (!ok) return;
    await withBusy(button, async () => {
      await bridge.apiPost("delegations/revoke", {
        session_id: sessionId,
        owner_user_id: button.dataset.owner,
      });
      toast("托管已撤销", "success");
      await refreshDetailViews(sessionId);
    });
  } else if (action === "deleg-force-choose") {
    const result = await promptForText({
      title: "强制代选",
      kicker: "FORCED CHOOSE",
      label: "选择当前行动角色的选项（A/B/C/D）",
      placeholder: "例如：B",
      required: true,
    });
    if (!result) return;
    const ok = await confirmAction("强制代选？", "该操作会直接消费当前角色的行动选项并生成剧情。", "确认代选");
    if (!ok) return;
    await withBusy(button, async () => {
      const resp = await bridge.apiPost("delegations/forced-choose", {
        session_id: sessionId,
        choice_key: result.trim().toUpperCase(),
        operation_id: `forced:${Date.now()}`,
      });
      if (resp?.ok === false) {
        toast(resp.message || "代选未完成", "error");
      } else if (resp?.notice_sent) {
        toast("已强制代选并通知群聊", "success");
      } else {
        toast("操作已提交，但群聊通知发送失败：" + (resp?.notice_reason || "未知原因"), "warn");
      }
      if (resp?.ok !== false && (resp?.story || resp?.turn)) {
        liveState.lastForcedResult = {
          story: resp.story || "",
          turn: resp.turn || "",
          at: new Date().toLocaleString(),
        };
      }
      await refreshDetailViews(sessionId);
    });
    } else if (action === "dm-cmd") {
    const cmd = button.dataset.dmCommand;
    const confirmNeeded = button.dataset.confirm === "1";
    if (confirmNeeded) {
      const ok = await confirmAction("执行 DM 操作？", button.dataset.hint || "该操作会修改副本状态。", "确认执行");
      if (!ok) return;
    }
    const payload = { session_id: sessionId, command: cmd };
    const doPost = async (p) => {
      await withBusy(button, async () => {
        await bridge.apiPost("dm/command", p);
        toast(`DM 操作完成：${cmd}`, "success");
        await refreshDetailViews(sessionId);
      });
    };
    const needsTarget =
      cmd === "whisper" ||
      cmd === "lock_action" ||
      cmd === "set_next_actor" ||
      cmd === "manual_roll";
    if (needsTarget && !(button.dataset.participant || button.dataset.target)) {
      const roster = detail.roster || [];
      const targetOptions = roster
        .filter((p) => p.group_user_id)
        .map(
          (p) =>
            `<option value="${escapeHTML(p.group_user_id)}">${escapeHTML(
              p.character_name || p.display_name || p.group_user_id,
            )}（${escapeHTML(p.group_user_id)}）</option>`,
        )
        .join("");
      const targetBody = `<div class="field"><label for="dm-target-user">目标角色（真实平台用户 ID）</label><select id="dm-target-user">${targetOptions}</select></div>`;
      let extra = "";
      if (cmd === "whisper") {
        extra = `<div class="field"><label for="dm-target-text">密语内容</label><textarea id="dm-target-text" rows="4"></textarea></div>`;
      } else if (cmd === "manual_roll") {
        extra = `<div class="field"><label for="dm-target-stat">检定名称（属性）</label><input id="dm-target-stat" /></div>
          <div class="field"><label for="dm-target-total">检定结果（数字）</label><input id="dm-target-total" type="number" /></div>`;
      } else if (cmd === "lock_action") {
        extra = `<label class="switch-field"><input id="dm-target-lock" type="checkbox" checked /><span><strong>锁定该角色行动</strong></span></label>`;
      } else if (cmd === "set_next_actor") {
        extra = `<p class="field-hint">将把目标角色设为当前行动者（若存在活跃选项会被作废）。</p>`;
      }
      openEditor({
        title: { whisper: "密语", lock_action: "锁定行动", set_next_actor: "指定下一位", manual_roll: "手动检定" }[cmd] || "DM 操作",
        kicker: "DM CONSOLE",
        body: targetBody + extra,
        saveLabel: "执行",
        onSave: async () => {
          const target = $("#dm-target-user")?.value;
          if (!target) throw new Error("请选择目标角色");
          payload.user_id = target;
          if (cmd === "whisper") {
            payload.text = $("#dm-target-text")?.value || "";
            if (!payload.text) throw new Error("请输入密语内容");
            payload.participant_id = target;
          } else if (cmd === "manual_roll") {
            payload.stat = ($("#dm-target-stat")?.value || "").trim();
            payload.total = Number($("#dm-target-total")?.value || 0);
            payload.participant_id = target;
            if (!payload.stat) throw new Error("请输入检定名称");
          } else if (cmd === "lock_action") {
            payload.participant_id = target;
            payload.locked = Boolean($("#dm-target-lock")?.checked);
          }
          await doPost(payload);
        },
      });
      return;
    }
    if (button.dataset.text && button.dataset.text !== "1") payload.text = button.dataset.text;
    if (cmd === "narrative" || cmd === "announce" || cmd === "whisper") {
      const text = await promptForText({
        title: "DM 输入",
        kicker: "DM CONSOLE",
        label: button.dataset.hint || "输入内容",
        multiline: true,
        required: true,
      });
      if (!text) return;
      payload.text = text;
    }
    if (cmd === "manual_roll") {
      const stat = await promptForText("检定名称（属性）");
      if (!stat) return;
      const total = await promptForText("检定结果（数字）");
      if (!total) return;
      payload.stat = stat.trim();
      payload.total = Number(total);
      payload.participant_id = button.dataset.participant || "";
    }
    if (cmd === "adjust_relationship") {
      const raw = await promptForText("格式：来源→目标 维度 增量，例如 队伍→npc:某人 信任 2");
      if (!raw) return;
      const parts = raw.trim().split(/\s+/);
      if (parts.length < 3) throw new Error("格式：来源→目标 维度 增量");
      const arrow = parts[0].split("→");
      payload.source = (arrow[0] || "").trim();
      payload.target = ((arrow[1] || parts[1])).trim();
      payload.dimension = parts.length > 3 ? parts.slice(1, -1).join(" ") : parts[1];
      payload.delta = Number(parts[parts.length - 1]);
    }
    await doPost(payload);
  } else if (action === "dm-lock-input") {
    await withBusy(button, async () => {
      await bridge.apiPost("dm/command", {
        session_id: sessionId,
        command: "lock_input",
        locked: button.dataset.locked !== "1",
      });
      toast("输入锁定状态已切换", "success");
      await refreshDetailViews(sessionId);
    });
  } else if (action === "dm-end-vote") {
    const winner = await promptForText("指定投票结果（A/B/C/D，留空=直接结束）");
    if (winner === null) return;
    await withBusy(button, async () => {
      await bridge.apiPost("dm/command", {
        session_id: sessionId,
        command: "force_end_vote",
        winner_key: winner.trim().toUpperCase(),
      });
      toast("投票已结束", "success");
      await refreshDetailViews(sessionId);
    });
  } else if (action === "dm-checkpoint") {
    await withBusy(button, async () => {
      await bridge.apiPost("dm/command", {
        session_id: sessionId,
        command: "checkpoint",
        name: "DM 手动检查点",
      });
      toast("检查点已创建", "success");
      await refreshDetailViews(sessionId);
    });
  } else if (action === "dm-master-toggle") {
    // A20: 人工 DM 总开关（启用/停用人工主持模式）。
    const currentlyEnabled = button.dataset.dmEnabled === "1";
    if (currentlyEnabled) {
      const ok = await confirmAction(
        "关闭人工 DM？",
        "将恢复 AI 自动主持，人工 DM 控制功能全部停用。",
        "确认关闭",
      );
      if (!ok) return;
      await withBusy(button, async () => {
        await bridge.apiPost("sessions/action", {
          session_id: sessionId,
          action: "dm_disable",
        });
        toast("已恢复 AI 自动模式", "success");
        await refreshDetailViews(sessionId);
      });
    } else {
      const dmUserId = await promptForText({
        title: "开启人工 DM",
        kicker: "DM CONTROL",
        label: "活动 DM 的 QQ 用户 ID",
        placeholder: "例如：123456789",
        required: true,
      });
      if (!dmUserId) return;
      await withBusy(button, async () => {
        await bridge.apiPost("sessions/action", {
          session_id: sessionId,
          action: "dm_enable",
          dm_user_id: dmUserId.trim(),
        });
        toast("人工 DM 已开启", "success");
        await refreshDetailViews(sessionId);
      });
    }
  } else if (action === "delete-session") {
    const name = detail.session.instance_name;
    const entered = await promptForText({
      title: "删除整个故事副本",
      kicker: "DANGER ZONE",
      label: `此操作会把整个故事副本移入回收目录。请输入副本名“${name}”确认：`,
      required: true,
    });
    if (entered === null) return;
    if (entered.trim() !== name) {
      throw new Error("输入的副本名不一致，未执行删除");
    }
    const ok = await confirmAction(
      `删除整个故事副本「${name}」？`,
      "角色、剧情、存档、Token 流水和独立 SQLite 都会从运行数据库移除，并尝试移入服务器回收目录。",
      "确认删除副本",
    );
    if (!ok) return;
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/action", {
        session_id: sessionId,
        action: "delete",
        confirm_name: name,
      });
      $("#session-modal").close();
      toast("整个故事副本已删除并移入回收目录", "success");
      await loadCore();
    });
  } else if (action === "save-state") {
    const worldState = parseJSONField("#session-state-json", "世界状态");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/state", {
        session_id: sessionId,
        revision: detail.session.revision,
        world_state: worldState,
      });
      toast("世界状态已保存，并生成安全快照", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "save-rules") {
    const rules = parseJSONField("#session-rules-json", "副本规则");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/rules", {
        session_id: sessionId,
        rules,
      });
      toast("副本规则、剧情进度与内容边界已保存", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-npc" || action === "edit-npc") {
    const npc =
      action === "edit-npc"
        ? (detail.session_characters || []).find(
            (item) => item.id === button.dataset.id,
          )
        : null;
    $("#session-modal").close();
    openSessionNPCEditor(sessionId, npc);
  } else if (action === "new-memory" || action === "edit-memory") {
    const memory =
      action === "edit-memory"
        ? (detail.memories || []).find((item) => item.id === button.dataset.id)
        : null;
    $("#session-modal").close();
    openMemoryEditor(memory, sessionId);
  } else if (action === "save-timing") {
    const current = detail.instance_config?.time_rules || {};
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/time-rules", {
        session_id: sessionId,
        rules: {
          ...current,
          card_code_ttl_seconds: readTimeValue("#t-card-code"),
          card_draft_ttl_seconds: readTimeValue("#t-card-draft"),
          card_completion_timeout_seconds: readTimeValue("#t-card-completion"),
          preparation_timeout_seconds: readTimeValue("#t-preparation"),
          ready_timeout_seconds: readTimeValue("#t-ready"),
          turn_timeout_seconds: readTimeValue("#t-turn"),
          turn_reminder_seconds: readTimeValue("#t-turn-reminder"),
          max_consecutive_timeouts: Number($("#t-timeout-count").value),
          standby_timeout_seconds: readTimeValue("#t-standby"),
          delegation_ttl_seconds: readTimeValue("#t-delegation"),
          all_idle_pause_seconds: readTimeValue("#t-all-idle"),
          vote_round_one_seconds: readTimeValue("#t-vote-one"),
          vote_round_two_seconds: readTimeValue("#t-vote-two"),
          vote_reminder_seconds: readTimeValue("#t-vote-reminder"),
          pause_stops_clock: $("#t-pause-clock").checked,
          announce_timeouts: $("#t-announce").checked,
          turn_timeout_action: $("#t-turn-action").value,
          card_timeout_action: $("#t-card-action").value,
          ready_timeout_action: $("#t-ready-action").value,
        },
      });
      toast("当前副本的时间与流程规则已保存", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "card-approve" || action === "card-reject") {
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/card-review", {
        session_id: sessionId,
        participant_ref: button.dataset.ref,
        approved: action === "card-approve",
        note: action === "card-approve" ? "WebUI 审核通过" : "WebUI 审核拒绝",
      });
      toast(action === "card-approve" ? "角色卡已通过" : "角色卡已拒绝", "success");
      await openSessionDetail(sessionId);
    });
  } else if (["designate", "retire", "ban"].includes(action)) {
    if (action !== "designate") {
      const ok = await confirmAction(
        action === "ban" ? "封禁并安全退场？" : "让该角色安全退场？",
        action === "ban"
          ? "将原子移出队列、撤销授权、取消未完成建卡并保留中性退场记录。"
          : "席位会释放，角色卡与剧情历史会归档保留，可按返场流程回来。",
        action === "ban" ? "封禁" : "安全退场",
      );
      if (!ok) return;
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/participant", {
        session_id: sessionId,
        participant_ref: button.dataset.ref,
        action,
        scope: "instance",
        reason: action === "ban" ? "WebUI 副本级封禁" : "WebUI 安全退场",
      });
      toast(
        action === "designate"
          ? "当前行动者已指定"
          : action === "ban"
            ? "封禁与退场已完成"
            : "角色已安全退场",
        "success",
      );
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "grant-role") {
    const userId = $("#permission-user-id").value.trim();
    if (!userId) throw new Error("请填写真实用户 ID");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/permission", {
        session_id: sessionId,
        user_id: userId,
        role: $("#permission-role").value,
      });
      toast("副本权限已授予", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-player") {
    $("#session-modal").close();
    openPlayerEditor(sessionId);
  } else if (action === "edit-player") {
    const player = detail.players.find((item) => item.id === button.dataset.id);
    $("#session-modal").close();
    openPlayerEditor(sessionId, player);
  } else if (action?.startsWith("turn-")) {
    const userId = button.dataset.userId;
    const order = (detail.turn?.order || []).map((item) => item.user_id);
    const position = order.indexOf(userId);
    if (action === "turn-add" && position < 0) {
      order.push(userId);
    } else if (action === "turn-remove" && position >= 0) {
      order.splice(position, 1);
    } else if (action === "turn-up" && position > 0) {
      [order[position - 1], order[position]] = [
        order[position],
        order[position - 1],
      ];
    } else if (action === "turn-down" && position >= 0 && position < order.length - 1) {
      [order[position], order[position + 1]] = [
        order[position + 1],
        order[position],
      ];
    } else {
      return;
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/turn-order", {
        session_id: sessionId,
        order,
      });
      toast("多人回合顺序已更新", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-save") {
    $("#session-modal").close();
    openSnapshotEditor(sessionId);
  } else if (action === "restore-save") {
    const snapshot = detail.snapshots.find((item) => item.id === button.dataset.id);
    const ok = await confirmAction(
      `恢复存档「${snapshot?.name || ""}」？`,
      "当前状态会先生成安全快照；恢复后旧分支不会再进入模型上下文，且会话自动暂停。",
      "恢复存档",
    );
    if (!ok) return;
    await bridge.apiPost("snapshots/restore", {
      session_id: sessionId,
      snapshot_ref: button.dataset.id,
    });
    toast("存档已恢复，会话现已暂停", "success");
    await loadCore();
    await openSessionDetail(sessionId);
  } else if (action === "delete-save") {
    const ok = await confirmAction(
      "删除这个存档？",
      "此操作不会改变当前世界状态，但删除后不能再用它恢复。",
      "删除存档",
    );
    if (!ok) return;
    await bridge.apiPost("snapshots/delete", { id: button.dataset.id });
    toast("存档已删除", "success");
    await openSessionDetail(sessionId);
  } else if (action === "delete-independent-save") {
    const filename = button.dataset.filename;
    const ok = await confirmAction(
      `删除独立存档文件「${filename}」？`,
      "文件会移入当前副本的回收目录；最终保护存档不会被允许删除。",
      "删除独立存档",
    );
    if (!ok) return;
    await bridge.apiPost("archives/delete", {
      session_id: sessionId,
      kind: "save",
      filename,
    });
    toast("独立存档文件已移入回收目录", "success");
    await openSessionDetail(sessionId);
  }
}

async function handleMemoryAction(button) {
  const memory = app.memories.find((item) => item.id === button.dataset.id);
  if (!memory) return;
  if (button.dataset.memoryAction === "edit") {
    openMemoryEditor(memory);
  } else {
    const ok = await confirmAction(
      "删除这条长期记忆？",
      "删除后叙事引擎不会再主动检索到这条事实。",
      "删除记忆",
    );
    if (!ok) return;
    await bridge.apiPost("memories/delete", { id: memory.id });
    toast("记忆已删除", "success");
    await loadMemories();
  }
}

function debounceRefresh() {
  window.clearTimeout(app.refreshTimer);
  app.refreshTimer = window.setTimeout(() => {
    loadCore().catch(showError);
    if (app.view === "memories") loadMemories().catch(showError);
    if (app.view === "audit") loadAudit().catch(showError);
  }, 450);
}

// A14（审计 #13）：总览轻量刷新（只拉 overview，不动设置/世界列表）。
async function refreshOverviewLight() {
  app.overview = await bridge.apiGet("overview");
  renderOverview();
}

async function startSSE() {
  try {
    app.sseId = await bridge.subscribeSSE("events", {
      onOpen() {
        setConnection("live", "实时连接");
      },
      onMessage(event) {
        const msg = event.parsed;
        if (!msg || msg.type === "keepalive") return;
        // A14（审计 #13）：按事件作用域局部刷新，避免每次事件整页重渲染。
        if (msg.type === "session") {
          if (liveState.sessionId && msg.session_id === liveState.sessionId) {
            refreshLiveTimers().catch(() => {});
            window.clearTimeout(liveState.sseRefresh);
            liveState.sseRefresh = window.setTimeout(
              () => refreshLiveDetail().catch(() => {}),
              300,
            );
          }
          if (app.view === "sessions" || app.view === "session_detail") {
            debounceRefresh();
          } else if (app.view === "dashboard") {
            refreshOverviewLight().catch(showError);
          }
          return;
        }
        if (msg.type === "settings") {
          loadCore().catch(showError);
          return;
        }
        if (msg.type === "backup") {
          toast("备份数据已更新", "info");
          return;
        }
        // A17：副本相关事件（turn/vote/dm/delegation/economy/ooc 等）触发 LIVE 轻量刷新。
        if (
          liveState.sessionId &&
          msg.session_id === liveState.sessionId &&
          ["turn", "vote", "dm", "delegation", "economy", "ooc", "participant", "card"].includes(
            msg.type,
          )
        ) {
          window.clearTimeout(liveState.sseRefresh);
          liveState.sseRefresh = window.setTimeout(
            () => refreshLiveDetail().catch(() => {}),
            300,
          );
          return;
        }
        debounceRefresh();
      },
      onError() {
        setConnection("error", "实时流断开");
      },
    });
  } catch (error) {
    console.warn("SSE unavailable", error);
    setConnection("live", "轮询模式");
  }
}

document.addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-view]");
  if (nav) switchView(nav.dataset.view);
  const jump = event.target.closest("[data-jump]");
  if (jump) switchView(jump.dataset.jump);

  const deliveryButton = event.target.closest("[data-delivery-action]");
  if (deliveryButton) {
    try {
      await withBusy(deliveryButton, async () => {
        const action = deliveryButton.dataset.deliveryAction;
        const response = await bridge.apiPost("deliveries", {
          action,
          delivery_id: deliveryButton.dataset.deliveryId,
        });
        if (action === "retry" && !response.ok) {
          toast(response.delivery?.reason || "平台仍无法主动推送，通知继续保留", "warning");
        } else {
          toast(action === "dismiss" ? "通知已忽略" : "通知已发送", "success");
        }
        await loadDeliveries();
      });
    } catch (error) {
      showError(error);
    }
    return;
  }

  const timePreset = event.target.closest("[data-time-preset]");
  if (timePreset) {
    applyTimePreset(timePreset.dataset.timePreset);
  }

  const openSession = event.target.closest("[data-open-session]");
  if (openSession) {
    try {
      await openSessionDetail(openSession.dataset.openSession);
    } catch (error) {
      showError(error);
    }
  }

  const worldButton = event.target.closest("[data-world-action]");
  if (worldButton) {
    try {
      await handleWorldAction(worldButton);
    } catch (error) {
      showError(error);
    }
  }

  const groupButton = event.target.closest("[data-group-action]");
  if (groupButton) {
    try {
      if (groupButton.dataset.groupAction === "remark") {
        openGroupRemarkEditor(groupButton);
      } else if (groupButton.dataset.groupAction === "token-quota") {
        await openGroupTokenQuotaEditor(groupButton);
      }
    } catch (error) {
      showError(error);
    }
  }

  const characterButton = event.target.closest("[data-character-action]");
  if (characterButton) {
    try {
      await handleCharacterAction(characterButton);
    } catch (error) {
      showError(error);
    }
  }

  const sessionButton = event.target.closest("[data-session-action]");
  if (sessionButton) {
    try {
      const action = sessionButton.dataset.sessionAction;
      if (action === "detail") {
        await openSessionDetail(sessionButton.dataset.id);
      } else {
        await runSessionAction(sessionButton.dataset.id, action, sessionButton);
      }
    } catch (error) {
      showError(error);
    }
  }

  // 0.12.0-A3（#2）：从群会话卡片直达副本实时仪表盘。
  const liveButton = event.target.closest("[data-session-live]");
  if (liveButton) {
    openLiveDashboard(liveButton.dataset.sessionLive);
    return;
  }

  const tab = event.target.closest("[data-session-tab]");
  if (tab) {
    const name = tab.dataset.sessionTab;
    $$("[data-session-tab]").forEach((item) =>
      item.classList.toggle("is-active", item.dataset.sessionTab === name),
    );
    $$("[data-session-tab-panel]").forEach((panel) =>
      panel.classList.toggle(
        "is-active",
        panel.dataset.sessionTabPanel === name,
      ),
    );
  }

  const detailAction = event.target.closest("[data-session-detail-action]");
  if (detailAction) {
    try {
      await handleSessionDetailAction(detailAction);
    } catch (error) {
      showError(error);
    }
  }

  const timerPolicyAction = event.target.closest("[data-timer-policy]");
  if (timerPolicyAction) {
    try {
      const sessionId = app.currentSession?.session?.id;
      if (!sessionId) return;
      await withBusy(timerPolicyAction, async () => {
        await bridge.apiPost("sessions/timer-policy", {
          session_id: sessionId,
          timer_type: timerPolicyAction.dataset.timerPolicy,
          enabled: timerPolicyAction.dataset.enabled === "true",
        });
        toast("倒计时开关已更新", "success");
        await openSessionDetail(sessionId);
      });
    } catch (error) {
      showError(error);
    }
  }

  const timerAction = event.target.closest("[data-timer-action]");
  if (timerAction) {
    try {
      let seconds = 0;
      if (timerAction.dataset.timerAction === "extend") {
        const raw = await promptForText({
          title: "延长计时器",
          kicker: "TIMER",
          label: "延长多少秒？",
          defaultValue: "1800",
          required: true,
        });
        if (raw === null) return;
        seconds = Number(raw);
        if (!Number.isInteger(seconds) || seconds <= 0) {
          throw new Error("延长秒数必须是正整数");
        }
      }
      if (timerAction.dataset.timerAction === "expire") {
        const ok = await confirmAction(
          "立即触发这个计时器？",
          "系统会立刻按当前超时规则处理对应玩家或投票。",
          "立即到期",
        );
        if (!ok) return;
      }
      await withBusy(timerAction, async () => {
        await bridge.apiPost("sessions/timer", {
          timer_id: timerAction.dataset.id,
          action: timerAction.dataset.timerAction,
          seconds,
        });
        toast("计时器已更新", "success");
        await openSessionDetail(app.currentSession.session.id);
      });
    } catch (error) {
      showError(error);
    }
  }

  const memoryButton = event.target.closest("[data-memory-action]");
  if (memoryButton) {
    try {
      await handleMemoryAction(memoryButton);
    } catch (error) {
      showError(error);
    }
  }

  const fallbackButton = event.target.closest("[data-fallback-action]");
  if (fallbackButton) {
    const values = [
      ...$("#fallback-provider-list").querySelectorAll(
        "[data-fallback-provider]",
      ),
    ].map((item) => item.value);
    const index = Number(fallbackButton.dataset.index);
    const action = fallbackButton.dataset.fallbackAction;
    if (action === "remove") {
      values.splice(index, 1);
    } else if (action === "up" && index > 0) {
      [values[index - 1], values[index]] = [
        values[index],
        values[index - 1],
      ];
    } else if (action === "down" && index < values.length - 1) {
      [values[index], values[index + 1]] = [
        values[index + 1],
        values[index],
      ];
    }
    renderFallbackProviders(values);
    $("#settings-dirty-text").textContent = "存在尚未保存的更改";
  }
});

$("#menu-toggle").addEventListener("click", () => {
  $("#sidebar").classList.toggle("is-open");
});

$("#refresh-button").addEventListener("click", async (event) => {
  try {
    await withBusy(event.currentTarget, async () => {
      await loadCore();
      if (app.view === "memories") await loadMemories();
      if (app.view === "audit") await loadAudit();
    });
    toast("数据已刷新", "success");
  } catch (error) {
    showError(error);
  }
});

$("#show-archived-worlds").addEventListener("change", async () => {
  try {
    const result = await bridge.apiGet("worlds", {
      include_archived: $("#show-archived-worlds").checked,
    });
    app.worlds = result.items || [];
    renderWorlds();
  } catch (error) {
    showError(error);
  }
});

$("#session-search").addEventListener("input", (event) => {
  window.clearTimeout(app.sessionSearchTimer);
  const value = event.currentTarget.value;
  app.sessionSearchTimer = window.setTimeout(async () => {
    app.sessionQuery = value.trim();
    app.sessionPage = 1;
    try {
      await loadSessionPage();
    } catch (error) {
      showError(error);
    }
  }, 280);
});
$("#session-search-scope").addEventListener("change", async (event) => {
  app.sessionScope = event.currentTarget.value;
  app.sessionPage = 1;
  try {
    await loadSessionPage();
  } catch (error) {
    showError(error);
  }
});
$("#session-search-clear").addEventListener("click", async () => {
  $("#session-search").value = "";
  $("#session-search-scope").value = "all";
  app.sessionQuery = "";
  app.sessionScope = "all";
  app.sessionPage = 1;
  try {
    await loadSessionPage();
  } catch (error) {
    showError(error);
  }
});
$("#session-page-prev").addEventListener("click", async () => {
  if (app.sessionPage <= 1) return;
  app.sessionPage -= 1;
  try {
    await loadSessionPage();
  } catch (error) {
    app.sessionPage += 1;
    showError(error);
  }
});
$("#session-page-next").addEventListener("click", async () => {
  if (app.sessionPage >= app.sessionPages) return;
  app.sessionPage += 1;
  try {
    await loadSessionPage();
  } catch (error) {
    app.sessionPage -= 1;
    showError(error);
  }
});

$("#new-world-button").addEventListener("click", () => openWorldEditor());
$("#import-world-package-button").addEventListener("click", openWorldPackageImportDialog);
$("#import-npc-button").addEventListener("click", openNpcImportDialog);
$("#new-session-button").addEventListener("click", openSessionCreator);
$("#new-memory-button").addEventListener("click", () => openMemoryEditor());
$("#add-fallback-provider").addEventListener("click", () => {
  const values = [
    ...$("#fallback-provider-list").querySelectorAll(
      "[data-fallback-provider]",
    ),
  ].map((item) => item.value);
  if (values.length >= 4) {
    toast("原生配置兼容模式最多提供 4 个备用模型", "error");
    return;
  }
  const primary = $("#setting-provider").value;
  const candidate = app.providers.find(
    (item) => item.id !== primary && !values.includes(item.id),
  );
  values.push(candidate?.id || "");
  renderFallbackProviders(values);
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
});
$("#memory-session-select").addEventListener("change", () =>
  loadMemories().catch(showError),
);
$("#memory-search").addEventListener("input", () => {
  window.clearTimeout(app.memoryTimer);
  app.memoryTimer = window.setTimeout(() => loadMemories().catch(showError), 280);
});
$("#audit-session-select").addEventListener("change", () =>
  loadAudit().catch(showError),
);
$("#audit-refresh-button").addEventListener("click", () =>
  loadAudit().catch(showError),
);

$("#editor-modal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.id !== "editor-save-button") return;
  if (!app.editorSave) {
    $("#editor-modal").close();
    return;
  }
  const button = $("#editor-save-button");
  try {
    await withBusy(button, app.editorSave);
    $("#editor-modal").close();
  } catch (error) {
    showError(error);
  }
});

function closeEditor() {
  app.editorSave = null;
  $("#editor-modal").close("cancel");
}

$("#editor-modal-close").addEventListener("click", closeEditor);
$("#editor-cancel-button").addEventListener("click", closeEditor);
$("#editor-modal").addEventListener("cancel", () => {
  app.editorSave = null;
});
$("#editor-modal").addEventListener("close", () => {
  app.editorSave = null;
});

$("#session-modal-close").addEventListener("click", () =>
  $("#session-modal").close(),
);

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  try {
    await withBusy(button, async () => {
      const result = await bridge.apiPost("settings/save", collectSettings());
      app.settings = result.settings;
      toast("设置已保存并立即生效", "success");
      await loadCore();
    });
  } catch (error) {
    showError(error);
  }
});

$("#settings-form").addEventListener("input", () => {
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
});

$("#export-backup-button").addEventListener("click", async (event) => {
  try {
    await withBusy(event.currentTarget, async () => {
      await bridge.download(
        "backup/export",
        {},
        "backup_tavern_v0.12.0.zip",
      );
    });
    toast("备份已生成", "success");
  } catch (error) {
    showError(error);
  }
});

$("#import-backup-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const mode = $("#import-mode").value;
  try {
    if (mode === "replace") {
      const ok = await confirmAction(
        "覆盖全部酒馆数据？",
        "现有世界、会话、记忆、存档与审计会被备份文件替换。请确保已经另行导出当前数据。",
        "确认覆盖",
      );
      if (!ok) return;
    }
    await bridge.upload(`backup/import/${mode}`, file);
    toast("备份导入完成", "success");
    await loadCore();
  } catch (error) {
    showError(error);
  } finally {
    event.target.value = "";
  }
});

window.addEventListener("beforeunload", () => {
  if (app.sseId) bridge.unsubscribeSSE(app.sseId);
  if (app.contextOff) app.contextOff();
});

function _readImportSource(fileInput, text) {
  return new Promise((resolve, reject) => {
    if (fileInput.files && fileInput.files[0]) {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsText(fileInput.files[0]);
    } else {
      resolve(text);
    }
  });
}

function isWorldImportConflict(error) {
  return /^导入冲突/.test(String(error?.message || ""));
}

async function submitWorldImport(parsed, importMode) {
  const resp = await bridge.apiPost("worlds/import", {
    world: parsed,
    import_mode: importMode,
  });
  const world = resp?.item || (app.worlds || []).find((w) => w.slug === parsed.slug);
  const importLabels = {
    updated: "世界包已更新",
    copied: "世界包已另存副本",
    identical: "相同内容已存在",
    created: "世界包已导入并创建世界",
  };
  toast(importLabels[resp?.mode] || "世界包导入完成", "success");
  await loadCore();
  return world;
}

// 0.11.1：导入遇到「同 slug、同内容版本但内容不同」冲突时，
// 弹出明确的三选一决策弹窗（覆盖为新修订 / 另存副本 / 取消），
// 而不是只把 409 错误文案原样展示。
function openWorldImportConflictDialog(parsed) {
  openEditor({
    title: "世界包导入冲突",
    kicker: "WORLD CONFLICT",
    body: `
      <p class="field-hint">同一 slug 已存在且内容版本相同，但内容不同。为避免静默覆盖，请选择处理方式：</p>
      <div class="import-actions">
        <button type="button" class="button button-primary" id="wc-override">覆盖为新修订</button>
        <button type="button" class="button" id="wc-copy">另存副本</button>
        <button type="button" class="action-button" id="wc-cancel">取消</button>
      </div>
      <div class="import-result" id="wc-result" hidden></div>
    `,
  });
  const finish = async (mode) => {
    const resultEl = $("#wc-result");
    resultEl.hidden = true;
    resultEl.className = "import-result";
    try {
      const world = await submitWorldImport(parsed, mode);
      $("#editor-modal").close();
      if (world) openWorldEditor(world);
    } catch (error) {
      resultEl.hidden = false;
      resultEl.classList.add("is-error");
      resultEl.textContent = error?.message || String(error);
      showError(error);
    }
  };
  $("#wc-override").addEventListener("click", () => finish("force_revision"));
  $("#wc-copy").addEventListener("click", () => finish("copy"));
  $("#wc-cancel").addEventListener("click", () => $("#editor-modal").close());
}

function openWorldPackageImportDialog() {
  const body = `
    <p class="field-hint">导入<b>世界包</b> JSON（必须包含 <code>slug</code> / <code>name</code> / <code>system_prompt</code>）。按 <code>slug</code> 新建或更新世界，导入后会<strong>直接打开该世界</strong>。</p>
    <div class="field">
      <label for="wp-import-file">世界包 JSON 文件</label>
      <input type="file" id="wp-import-file" accept=".json,application/json" />
    </div>
    <div class="field">
      <label for="wp-import-mode">同一 slug 的处理方式</label>
      <select id="wp-import-mode">
        <option value="auto">自动判断（推荐）</option>
        <option value="force_revision">覆盖为同一世界的新修订</option>
        <option value="copy">另存为独立副本</option>
      </select>
      <small>内容完全相同不会重复导入；更新会保留 WORLD 编号，副本会获得新编号。</small>
    </div>
    <div class="field">
      <label for="wp-import-text">或粘贴 JSON 文本</label>
      <textarea id="wp-import-text" rows="12" placeholder='将世界包 JSON 粘贴到这里…'></textarea>
    </div>
    <div class="import-actions">
      <button type="button" class="button button-primary" id="wp-import-submit">导入并创建世界</button>
      <button type="button" class="action-button" id="wp-import-cancel">取消</button>
    </div>
    <div class="import-result" id="wp-import-result" hidden></div>
  `;
  openEditor({ title: "导入世界包", kicker: "WORLD IMPORT", body });

  $("#wp-import-cancel").addEventListener("click", () => $("#editor-modal").close());
  $("#wp-import-submit").addEventListener("click", async () => {
    const resultEl = $("#wp-import-result");
    resultEl.hidden = true;
    resultEl.className = "import-result";
    const raw = await _readImportSource($("#wp-import-file"), $("#wp-import-text").value.trim());
    try {
      if (!raw) throw new Error("请选择 JSON 文件或在文本框中粘贴内容");
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (parseError) {
        throw new Error(`JSON 解析失败：${parseError.message}`);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("世界包必须是包含 slug / name / system_prompt 的 JSON 对象");
      }
      const submit = $("#wp-import-submit");
      submit.disabled = true;
      try {
        const world = await submitWorldImport(parsed, $("#wp-import-mode").value);
        if (world) {
          openWorldEditor(world);
        } else {
          $("#editor-modal").close();
        }
      } finally {
        submit.disabled = false;
      }
    } catch (error) {
      if (isWorldImportConflict(error)) {
        openWorldImportConflictDialog(parsed);
        return;
      }
      resultEl.hidden = false;
      resultEl.classList.add("is-error");
      resultEl.textContent = error?.message || String(error);
      showError(error);
    }
  });
}

function openNpcImportDialog() {
  const body = `
    <p class="field-hint">导入<b>常驻 NPC</b>：JSON 为角色数组，或包含 <code>items</code> / <code>npcs</code> / <code>characters</code> 字段的对象。将批量写入下方选择的目标世界。</p>
    <div class="field">
      <label for="np-import-file">常驻 NPC JSON 文件</label>
      <input type="file" id="np-import-file" accept=".json,application/json" />
    </div>
    <div class="field">
      <label for="np-import-text">或粘贴 JSON 文本</label>
      <textarea id="np-import-text" rows="12" placeholder='将常驻 NPC 的 JSON 粘贴到这里…'></textarea>
    </div>
    <div class="field">
      <label for="np-import-world">目标世界</label>
      <select id="np-import-world"></select>
    </div>
    <div class="import-actions">
      <button type="button" class="button button-primary" id="np-import-submit">导入</button>
      <button type="button" class="action-button" id="np-import-cancel">取消</button>
    </div>
    <div class="import-result" id="np-import-result" hidden></div>
  `;
  openEditor({ title: "导入常驻 NPC", kicker: "NPC IMPORT", body });

  const worldSelect = $("#np-import-world");
  worldSelect.innerHTML = (app.worlds || [])
    .map(
      (w) =>
        `<option value="${escapeHTML(w.id)}">${escapeHTML(w.name)}（${escapeHTML(w.slug)}）</option>`,
    )
    .join("");
  if (!app.worlds.length) {
    worldSelect.innerHTML =
      '<option value="">（暂无世界，请先导入或新建世界）</option>';
  }

  $("#np-import-cancel").addEventListener("click", () => $("#editor-modal").close());
  $("#np-import-submit").addEventListener("click", async () => {
    const resultEl = $("#np-import-result");
    resultEl.hidden = true;
    resultEl.className = "import-result";
    const raw = await _readImportSource($("#np-import-file"), $("#np-import-text").value.trim());
    try {
      if (!raw) throw new Error("请选择 JSON 文件或在文本框中粘贴内容");
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (parseError) {
        throw new Error(`JSON 解析失败：${parseError.message}`);
      }
      const worldId = worldSelect.value;
      if (!worldId) throw new Error("请选择目标世界");
      const payload = Array.isArray(parsed)
        ? { world_id: worldId, items: parsed }
        : { world_id: worldId, ...parsed };
      if (
        !Array.isArray(payload.items) &&
        !Array.isArray(payload.npcs) &&
        !Array.isArray(payload.characters)
      ) {
        throw new Error(
          "常驻 NPC 必须是数组，或包含 items / npcs 字段的对象",
        );
      }
      const submit = $("#np-import-submit");
      submit.disabled = true;
      try {
        const res = await bridge.apiPost("characters/import", payload);
        const count = res?.created ?? (res?.items || []).length;
        toast(`已导入 ${count} 个常驻角色`, "success");
        await loadCore();
        await openCharacterManager(worldId);
        $("#editor-modal").close();
      } finally {
        submit.disabled = false;
      }
    } catch (error) {
      resultEl.hidden = false;
      resultEl.classList.add("is-error");
      resultEl.textContent = error?.message || String(error);
      showError(error);
    }
  });
}

// ── A18：统一详情上下文（SESSION INSPECTOR 与 LIVE 共用）─────────────────
function resolveLiveDetailContext() {
  if (app.currentSession?.session?.id) {
    return {
      detail: app.currentSession,
      sessionId: app.currentSession.session.id,
    };
  }
  if (liveState.detail?.session?.id) {
    return { detail: liveState.detail, sessionId: liveState.detail.session.id };
  }
  const sid = $("#live-session-picker")?.value;
  if (sid) return { detail: liveState.detail, sessionId: sid };
  return null;
}

async function refreshDetailViews(sessionId) {
  const jobs = [];
  if (app.currentSession?.session?.id === sessionId) {
    jobs.push(openSessionDetail(sessionId));
  }
  if (liveState.sessionId === sessionId) {
    jobs.push(refreshLiveDetail());
  }
  if (jobs.length) await Promise.all(jobs);
}

// ── v0.12.0：副本实时仪表盘 ─────────────────────────────────────────
const liveState = {
  sessions: [],
  sessionId: "",
  detail: null,
  timeline: null,
  timer: null,
  timerOrder: "desc",
};

const TIMER_TYPE_LABELS = {
  turn: "个人回合",
  vote: "集体投票",
  card_code: "建卡码",
  card_draft: "角色草稿",
  card_completion: "建卡确认",
  preparation: "准备大厅",
  ready: "准备确认",
  standby: "候补退场",
  delegation: "代控授权",
  all_idle: "全员空闲",
};

function liveTimerLabel(type) {
  return TIMER_TYPE_LABELS[type] || type || "计时器";
}

async function loadSessionDashboard() {
  const resp = await bridge.apiGet("dashboard/sessions");
  liveState.sessions = resp.sessions || [];
  const picker = $("#live-session-picker");
  const previous = picker.value;
  picker.innerHTML =
    '<option value="">选择副本…</option>' +
    liveState.sessions
      .map(
        (session) =>
          `<option value="${escapeHTML(session.id)}">${escapeHTML(
            session.name || session.id,
          )}（${escapeHTML(statusLabel(session.state))}）</option>`,
      )
      .join("");
  // 优先保留进入视图时预选（来自群会话卡片的「实时」入口）；否则自动选中运行中的副本。
  if (
    previous &&
    liveState.sessions.some((session) => session.id === previous)
  ) {
    picker.value = previous;
  } else if (liveState.sessions.length) {
    const running =
      liveState.sessions.find((item) => item.state === "running") ||
      liveState.sessions[0];
    picker.value = running.id;
  }
  if (picker.value) {
    await refreshLiveDetail();
  } else {
    $("#live-detail-root").innerHTML = `
      <div class="empty-state compact">
        <div class="empty-symbol">◔</div>
        <span>尚未建立任何副本，请先在「群会话」中开馆</span>
      </div>`;
  }
}

// ── 0.12.0-A3（#2）：从群会话卡片直达副本实时仪表盘 ────────────────
function openLiveDashboard(sessionId) {
  const picker = $("#live-session-picker");
  if (picker) picker.value = sessionId;
  switchView("session_detail");
}

async function refreshLiveDetail() {
  const sessionId = $("#live-session-picker").value;
  if (!sessionId) {
    $("#live-detail-root").innerHTML = `
      <div class="empty-state compact">
        <div class="empty-symbol">◔</div>
        <span>请选择要查看的副本</span>
      </div>`;
    return;
  }
  liveState.sessionId = sessionId;
  const [detail, timeline] = await Promise.all([
    bridge.apiGet("dashboard/session", { session_id: sessionId }),
    bridge.apiGet("dashboard/timeline", {
      session_id: sessionId,
      limit: 15,
    }),
  ]);
  liveState.detail = detail;
  liveState.timeline = timeline;
  renderLiveDetail();
}


// ═══════════════════════════════════════════════════════════════════════
// A11：受控世界状态可视化
// —— 除 facts 外的全部字段渲染为语义卡片（图标 + 子标题），
//    inventory / relationships / check_modifiers / progress 使用专用界面；
//    未知字段按类型（标量/列表/对象）自动兜底，兼容更多类型的世界包。
// ═══════════════════════════════════════════════════════════════════════
const WS_FIELD_ORDER = [
  "location", "time", "scene_summary", "progress", "check_modifiers",
  "inventory", "relationships", "party", "characters", "npcs",
  "weather", "flags", "events", "quests", "objectives", "stats",
];
const WS_FIELD_ICONS = {
  location: "📍", time: "🕒", scene_summary: "🖼️", progress: "📈",
  check_modifiers: "⚖️", inventory: "🎒", relationships: "🤝",
  party: "👥", characters: "🧙", npcs: "🧝", weather: "🌦️",
  flags: "🚩", events: "📜", quests: "⚔️", objectives: "🎯", stats: "📊",
};
const WS_FIELD_LABELS = {
  location: "当前位置", time: "当前时间", scene_summary: "场景摘要", progress: "剧情进度",
  check_modifiers: "检定修正", inventory: "背包 / 物品栏", relationships: "关系与好感度",
  party: "队伍", characters: "角色", npcs: "NPC", weather: "天气", flags: "剧情标记",
  events: "事件", quests: "任务", objectives: "目标", stats: "数值",
};
const WS_ITEM_ICON_RULES = [
  [/火把|蜡烛|灯/, "🔥"], [/绳索|钩爪|绳/, "🪢"], [/药|医疗|绷带|治疗/, "💊"],
  [/地图/, "🗺️"], [/徽章|令|文书|委任/, "🪪"], [/钥匙|钥匙扣/, "🗝️"],
  [/武器|剑|刀|匕首|弓|弩/, "⚔️"], [/盾|护甲|盔甲/, "🛡️"], [/食物|干粮|面包/, "🍞"],
  [/币|金币|银币|钱/, "💰"], [/信|信件|纸条|纸/, "✉️"], [/卷轴|书|笔记|册/, "📜"],
  [/工具|锹|锤|凿/, "🔧"], [/宝石|晶|矿石|矿/, "💎"], [/酒|麦酒|药水|剂/, "🧪"],
  [/符|符文|刻/, "🔮"], [/钟|铃/, "🔔"], [/骨|兽|皮|毛/, "🦴"],
];
function wsItemIcon(name) {
  const text = String(name || "");
  for (const [rule, icon] of WS_ITEM_ICON_RULES) {
    if (rule.test(text)) return icon;
  }
  return "🧰";
}
function wsOwnerLabel(owner, idLabels = {}) {
  const text = String(owner || "");
  if (text === "party_supplies") return { label: "队伍物资", icon: "🎒" };
  if (text === "quest_items") return { label: "任务物品", icon: "🗝️" };
  if (text === "party_inventory" || text === "party") {
    return { label: "队伍物资", icon: "🎒" };
  }
  const resolved = wsResolveId(text, idLabels);
  if (resolved) return { label: resolved, icon: "👤" };
  return { label: text, icon: "🎒" };
}
// A16：统一实体显示名解析（配合后端 entity_resolver）。
// 规则：精确匹配 → 剥离已知前缀匹配 → 后缀/uuid 匹配 → 降级名称（绝不回显完整内部 ID）。
function wsStripPrefix(id) {
  return String(id || "")
    .trim()
    .replace(
      /^(world:character_|world:entity_|world:|participant_|player_|session_character_|snpc_|npc:|npc_|character_|team_|party_)/,
      "",
    );
}
function wsIsReadableName(t) {
  if (/[\u4e00-\u9fff]/.test(t)) return true;
  if (t.includes(" ")) return true;
  if (t.length < 8) return true;
  return !/^[\w:._-]{8,}$/.test(t);
}
function wsFallbackEntityName(id) {
  const t = String(id || "").trim();
  if (!t) return "未知实体";
  if (/队伍|party|team/i.test(t)) return "队伍";
  if (/^(player_|participant_)/i.test(t)) return "已离开玩家";
  if (/^(npc:|npc_|snpc_|character_|world:)/i.test(t)) return "已删除实体";
  if (wsIsReadableName(t)) return t;
  const short = wsStripPrefix(t).slice(0, 8);
  return short ? `未知实体(${short})` : "未知实体";
}
function wsResolveId(id, idLabels = {}) {
  const text = String(id || "").trim();
  if (!text) return "";
  if (idLabels[text]) return idLabels[text];
  if (/队伍|party|team/i.test(text)) return "队伍";
  const stripped = wsStripPrefix(text);
  if (stripped !== text && idLabels[stripped]) return idLabels[stripped];
  if (stripped.length >= 8) {
    for (const key of Object.keys(idLabels)) {
      if (key.length > 8 && key.endsWith(stripped)) return idLabels[key];
    }
  }
  if (text.length >= 8) {
    for (const key of Object.keys(idLabels)) {
      if (key.length > 8 && key.endsWith(text)) return idLabels[key];
    }
  }
  return wsFallbackEntityName(text);
}
function wsTargetLabel(rawTarget, idLabels = {}) {
  const text = String(rawTarget || "");
  if (text.includes("→")) {
    const [left, right] = text.split("→", 2);
    const leftLabel = wsResolveId(left.trim(), idLabels);
    const rightLabel = wsResolveId(right.trim(), idLabels);
    const isPartyLeft = /队伍|party|team/i.test(left);
    return {
      label: `${isPartyLeft ? "队伍" : leftLabel} → ${rightLabel}`,
      isParty: isPartyLeft || /队伍|party|team/i.test(text),
      fallback: /未知实体|已离开|已删除/.test(leftLabel + rightLabel),
    };
  }
  if (/队伍|party|team/i.test(text)) return { label: "队伍", isParty: true };
  return { label: wsResolveId(text, idLabels), isParty: false };
}
function wsFavLevel(value) {
  const n = Number(value) || 0;
  if (n <= -5) return { label: "敌对", tone: "danger" };
  if (n <= -3) return { label: "冷淡", tone: "danger" };
  if (n <= -1) return { label: "疏远", tone: "warn" };
  if (n === 0) return { label: "中立", tone: "muted" };
  if (n <= 2) return { label: "友好", tone: "ok" };
  if (n <= 4) return { label: "信赖", tone: "ok" };
  return { label: "至交", tone: "ok" };
}
function wsFavRange(values) {
  const maxAbs = Math.max(
    1,
    ...values.map((item) => Math.abs(Number(item.value) || 0)),
  );
  return Math.max(4, Math.min(10, Math.ceil(maxAbs)));
}
function wsFavBar(trait, value, range) {
  const n = Number(value) || 0;
  const level = wsFavLevel(n);
  const half = (Math.abs(n) / range) * 50;
  // A24: 单行长条进度（标签 + 轨道 + 数值 + 等级徽章），可读性与审美优先。
  return `
    <div class="rel-fav-row" title="${escapeHTML(trait)} ${n > 0 ? "+" : ""}${n} · ${escapeHTML(
      level.label,
    )}">
      <span class="rel-fav-trait">${escapeHTML(trait)}</span>
      <span class="rel-bar-track">
        ${n < 0 ? `<i class="rel-bar neg" style="right:50%;width:${half}%"></i>` : ""}
        ${n > 0 ? `<i class="rel-bar pos" style="left:50%;width:${half}%"></i>` : ""}
      </span>
      <b class="rel-fav-value ${level.tone}">${n > 0 ? "+" : ""}${n}</b>
      <span class="rel-level-chip ${level.tone}">${level.label}</span>
    </div>`;
}
function renderWSProgress(progress) {
  const done = Number(progress?.completed_milestones) || 0;
  const total = Number(progress?.total_milestones) || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const chapter = progress?.chapter || "章节未设置";
  const objective = progress?.current_objective || "当前目标未设置";
  return `
    <div class="ws-progress">
      <div class="ws-progress-top">
        <strong>${escapeHTML(chapter)}</strong>
        <b>${pct}%</b>
      </div>
      <div class="ws-progress-bar"><i style="width:${pct}%"></i></div>
      <div class="ws-progress-meta">
        <span>里程碑 ${done} / ${total}</span>
        <span class="ws-objective">${escapeHTML(objective)}</span>
      </div>
    </div>`;
}
function renderWSInventory(inventory, idLabels = {}) {
  if (!inventory || typeof inventory !== "object") {
    return `<span class="character-card-empty">空背包</span>`;
  }
  const groups = Object.entries(inventory)
    .map(([owner, items]) => {
      const meta = wsOwnerLabel(owner, idLabels);
      const entries = items && typeof items === "object" ? Object.entries(items) : [];
      const rows = entries
        .map(([name, raw]) => {
          let qty = "";
          let category = "";
          let desc = "";
          if (raw !== null && typeof raw === "object" && !Array.isArray(raw)) {
            qty = String(
              raw.count ?? raw.quantity ?? raw.qty ?? raw.amount ?? 1,
            );
            category = String(raw.category || "");
            desc = String(raw.description || "");
          } else {
            qty = String(raw);
          }
          const cat = category || (
            owner === "quest_items" ? "任务物品" :
            owner === "party_supplies" ? "队伍补给" : "个人物品"
          );
          return `
            <div class="inv-item">
              <span class="inv-item-icon">${wsItemIcon(name)}</span>
              <div class="inv-item-main">
                <span class="inv-item-name">${escapeHTML(name)}</span>
                ${desc ? `<small class="inv-item-desc">${escapeHTML(desc)}</small>` : ""}
              </div>
              <span class="inv-cat">${escapeHTML(cat)}</span>
              <span class="inv-item-qty">× ${escapeHTML(qty)}</span>
            </div>`;
        })
        .join("");
      return `
        <section class="inv-group">
          <header class="inv-group-head">
            <span class="inv-group-icon">${meta.icon}</span>
            <strong>${escapeHTML(meta.label)}</strong>
            <span class="inv-count">${entries.length} 件</span>
          </header>
          <div class="inv-list">${rows || '<span class="character-card-empty">无物品</span>'}</div>
        </section>`;
    })
    .join("");
  return `<div class="inv-grid">${groups}</div>`;
}

// ── A16：经济 / 托管 / DM 控制台面板 ────────────────────────────────
function renderEconomyPanel(detail) {
  const econ = detail.economy || {};
  const currencies = econ.currencies || [];
  const wallets = econ.wallets || [];
  const recent = econ.recent || [];
  if (!currencies.length) {
    return `<div class="panel" style="margin-bottom:18px">
      <div class="panel-head"><div><div class="eyebrow">💰 ECONOMY</div><h2>经济系统（未启用）</h2></div>
      <button class="button button-primary" data-session-detail-action="econ-toggle" data-enabled="0" ${detail.readonly ? "disabled" : ""}>启用经济</button></div>
      <p class="field-hint">当前世界包未声明 rules.economy；启用后仍需世界包提供货币定义才会生效。</p></div>`;
  }
  const enabled = Boolean(econ.enabled);
  const precisionOf = (cid) => currencies.find((c) => c.currency_id === cid)?.precision || 0;
  const fmt = (minor, cid) => {
    const p = precisionOf(cid);
    return p ? (Number(minor || 0) / 10 ** p).toFixed(p) : String(minor || 0);
  };
  const partyWallets = wallets.filter((w) => w.owner_type === "party");
  const mainCards = currencies
    .filter((c) => c.public)
    .slice(0, 6)
    .map((c) => {
      const w = partyWallets.find((x) => x.currency_id === c.currency_id);
      return `<div class="econ-card"><div class="econ-card-head"><strong>${escapeHTML(c.icon || "")} ${escapeHTML(c.name)}</strong><span class="status-badge">${escapeHTML(c.short_name || c.currency_id)}</span></div>
        <div class="econ-balance">${enabled ? escapeHTML(fmt(w?.balance, c.currency_id)) : "—"}</div>
        <small>${escapeHTML(c.description || "")}</small>
        <div class="toolbar-actions">${enabled ? `
          <button class="action-button" data-session-detail-action="econ-adjust" data-currency="${escapeHTML(c.currency_id)}" data-owner-type="party" data-owner-ref="party" data-kind="credit" data-amount="10">+10</button>
          <button class="action-button is-danger" data-session-detail-action="econ-adjust" data-currency="${escapeHTML(c.currency_id)}" data-owner-type="party" data-owner-ref="party" data-kind="debit" data-amount="10">-10</button>` : ""}
        </div></div>`;
    })
    .join("");
  const recentRows = recent
    .slice(0, 6)
    .map((tx) => `<div class="econ-tx">${escapeHTML(tx.created_at || "")} · ${escapeHTML(tx.kind)} · ${escapeHTML(tx.currency_id)} · ${escapeHTML(String(tx.amount))} · ${escapeHTML(tx.reason || "")}</div>`)
    .join("");
  return `<div class="panel" style="margin-bottom:18px">
    <div class="panel-head"><div><div class="eyebrow">💰 ECONOMY</div><h2>经济系统</h2></div>
      <div class="toolbar-actions">
        <button class="action-button" data-session-detail-action="econ-tx">交易日志</button>
        <button class="action-button" data-session-detail-action="econ-toggle" data-enabled="${enabled ? "1" : "0"}" ${detail.readonly ? "disabled" : ""}>${enabled ? "关闭经济" : "启用经济"}</button>
      </div></div>
    <p class="field-hint">${enabled ? "已启用：世界包货币与钱包生效，AI 经济操作可用。" : "已停用：世界包未接入时不会创建货币/钱包。"}</p>
    <div class="econ-grid">${mainCards || '<span class="character-card-empty">无公开货币</span>'}</div>
    ${recentRows ? `<details class="tb-section"><summary class="tb-section-head" style="cursor:pointer">最近交易</summary><div style="display:flex;flex-direction:column;gap:4px">${recentRows}</div></details>` : ""}
  </div>`;
}

function renderDelegationPanel(detail) {
  const delegations = detail.delegations || [];
  const rows = delegations
    .map((d) => `<div class="deleg-row">
      <div class="deleg-main"><strong>${escapeHTML(d.participant_character || d.participant_display || d.participant_id)}</strong>
        <small>拥有者 ${escapeHTML(d.owner_user_id)} → 代理人 ${escapeHTML(d.delegate_user_id)} · ${escapeHTML(d.source || "player")} · ${escapeHTML(d.status)}${d.expires_at ? " · 至 " + escapeHTML(d.expires_at) : ""} · 权限 ${escapeHTML((d.permissions || []).join("/"))}</small></div>
      <button class="action-button is-danger" data-session-detail-action="deleg-revoke" data-owner="${escapeHTML(d.owner_user_id)}" ${detail.readonly ? "disabled" : ""}>撤销</button>
    </div>`)
    .join("");
  const roster = detail.roster || [];
  const grantButtons = roster
    .filter((p) => ["active", "reserved"].includes(p.participation_status))
    .map((p) => `<button class="action-button" data-session-detail-action="deleg-grant" data-owner="${escapeHTML(p.group_user_id)}" title="为 ${escapeHTML(p.character_name || p.group_user_id)} 指定代理人" ${detail.readonly ? "disabled" : ""}>托管 ${escapeHTML(p.character_name || p.group_user_id)}</button>`)
    .join("");
  return `<div class="panel" style="margin-bottom:18px">
    <div class="panel-head"><div><div class="eyebrow">🤝 CONTROL</div><h2>角色托管 / 代操作</h2></div>
      <div class="toolbar-actions">
        <button class="action-button" data-session-detail-action="deleg-force-choose" ${detail.readonly ? "disabled" : ""}>强制代选</button>
      </div></div>
    ${grantButtons ? `<div class="dm-btn-row" style="margin-bottom:8px">${grantButtons}</div>` : ""}
    ${rows ? `<div class="deleg-table">${rows}</div>` : '<span class="character-card-empty">当前没有托管记录</span>'}
  </div>`;
}

function renderDMConsole(detail) {
  const control = detail.control || { mode: "auto", active_dm_user_id: "", phase: "auto", beat_no: 0 };
  const pending = detail.pending_operations || [];
  const isDm = control.mode === "dm";
  // A20: 人工 DM 总开关——未激活时全部功能只读，避免误操作。
  const dmPolicy = detail.instance_config?.world_snapshot?.rules?.chat_experience?.dm || {};
  const can = (policyKey = "") =>
    isDm && !detail.readonly && (!policyKey || dmPolicy[policyKey] !== false);
  const disabled = (policyKey = "") => can(policyKey) ? "" : "disabled";
  const lockHint = isDm
    ? ""
    : `<p class="field-hint" style="margin:8px 0">🔒 人工 DM 未激活：请先在上方「开启人工 DM」总开关激活后使用这些功能。</p>`;
  return `
    <div class="dm-state-grid" style="margin-bottom:10px">
      <div class="tb-stat"><span>模式</span><strong>${isDm ? "人工 DM" : "AI 自动"}</strong></div>
      <div class="tb-stat"><span>活动 DM</span><strong>${escapeHTML(control.active_dm_user_id || "未指定")}</strong></div>
      <div class="tb-stat"><span>阶段</span><strong>${escapeHTML(control.phase || "auto")}</strong></div>
      <div class="tb-stat"><span>已推进段</span><strong>${escapeHTML(String(control.beat_no || 0))}</strong></div>
      <div class="tb-stat"><span>待处理任务</span><strong>${pending.length}</strong></div>
      <div class="tb-stat ${detail.session?.input_locked ? "is-warn" : ""}"><span>输入锁</span><strong>${detail.session?.input_locked ? "已锁定" : "未锁定"}</strong></div>
    </div>
    ${lockHint}
    <p class="field-hint">权限来源：副本 DM/管理员权限与世界包 DM 策略共同生效；禁用按钮可在世界包“多人群聊体验”中调整。</p>
    <div class="tb-section"><div class="tb-section-head">📜 剧情</div><div class="dm-btn-row">
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="narrative" title="输入要插入或覆盖的剧情正文；需要世界包允许改写叙事" ${disabled("allow_narrative_override")}>插入剧情</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="announce" title="向当前群会话发送主持公告" ${disabled()}>系统公告</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="whisper" title="选择目标角色并输入仅其可见的信息；需要私聊来源或待投递队列" ${disabled("allow_secret_whispers")}>密语</button>
    </div></div>
    <div class="tb-section"><div class="tb-section-head">🎯 行动 / 投票</div><div class="dm-btn-row">
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="set_next_actor" title="从当前阵容选择下一位行动角色" ${disabled("allow_state_intervention")}>指定下一位</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="lock_action" data-participant="" title="从当前阵容选择角色并锁定或解锁其行动" ${disabled("allow_state_intervention")}>锁定行动</button>
      <button class="action-button" data-session-detail-action="dm-end-vote" title="立即按当前有效票结束投票" ${disabled("allow_state_intervention")}>结束投票</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="manual_roll" data-participant="" title="从当前阵容选择角色并记录权威检定结果" ${disabled("allow_manual_checks")}>手动检定</button>
    </div></div>
    <div class="tb-section"><div class="tb-section-head">🔧 会话 / 状态</div><div class="dm-btn-row">
      <button class="action-button" data-session-detail-action="dm-lock-input" data-locked="${detail.session?.input_locked ? "1" : "0"}" title="临时阻止或恢复玩家提交行动" ${disabled("allow_state_intervention")}>${detail.session?.input_locked ? "解锁输入" : "锁定输入"}</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="checkpoint" title="创建可回滚的命名检查点" ${disabled()}>创建检查点</button>
      <button class="action-button" data-session-detail-action="dm-cmd" data-dm-command="adjust_relationship" title="格式：来源→目标、维度、增量；操作写入审计" ${disabled("allow_state_intervention")}>调整关系</button>
    </div></div>`;
}

function renderWSRelationships(relationships, idLabels = {}) {
  if (!relationships || typeof relationships !== "object") {
    return `<span class="character-card-empty">暂无关系数据</span>`;
  }
  const party = [];
  const individual = [];
  const info = [];
  const favValues = [];
  for (const [rawTarget, value] of Object.entries(relationships)) {
    if (typeof value === "string") {
      info.push({ target: wsTargetLabel(rawTarget, idLabels).label, text: value });
      continue;
    }
    if (value !== null && typeof value === "object") {
      const meta = wsTargetLabel(rawTarget, idLabels);
      const traits = Object.entries(value).filter(([, v]) => typeof v === "number");
      for (const [trait, v] of traits) favValues.push({ value: v });
      const row = { meta, traits };
      (meta.isParty ? party : individual).push(row);
      continue;
    }
    if (typeof value === "number") {
      const meta = wsTargetLabel(rawTarget, idLabels);
      const row = { meta, traits: [["好感", value]] };
      favValues.push({ value });
      (meta.isParty ? party : individual).push(row);
    }
  }
  const range = wsFavRange(favValues);
  // A16：关系卡片化——每条关系一张横向卡片，卡片内字段竖向排列。
  const section = (rows, icon) =>
    rows.length
      ? `<section class="rel-section">
          <header class="rel-section-head">${icon} <strong>${rows[0].meta.isParty ? "队伍好感度" : "角色好感度"}</strong></header>
          <div class="rel-card-grid">
          ${rows
            .map(
              (row) => `
                <article class="rel-card">
                  <div class="rel-card-head">
                    <span class="rel-card-icon">${row.meta.isParty ? "👥" : "🧑‍🤝‍🧑"}</span>
                    <div class="rel-card-title">
                      <strong>${escapeHTML(row.meta.label)}</strong>
                      <small>${row.meta.isParty ? "队伍关系" : "角色关系"}${
                        row.meta.fallback ? " · 名称待解析" : ""
                      }</small>
                    </div>
                  </div>
                  <div class="rel-card-body">
                    ${row.traits
                      .map(([trait, v]) => wsFavBar(trait, v, range))
                      .join("")}
                  </div>
                </article>`,
            )
            .join("")}
          </div>
        </section>`
      : "";
  const infoBlock = info.length
    ? `<section class="rel-section">
        <header class="rel-section-head">🏛️ <strong>组织 / 势力关系</strong></header>
        <div class="rel-info-grid">
          ${info
            .map(
              (item) => `
                <div class="rel-info">
                  <strong>${escapeHTML(item.target)}</strong>
                  <span>${escapeHTML(item.text)}</span>
                </div>`,
            )
            .join("")}
        </div>
      </section>`
    : "";
  return `
    <div class="rel-layout">
      ${section(party, "👥")}
      ${section(individual, "🧑‍🤝‍🧑")}
      ${infoBlock}
      ${
        !party.length && !individual.length && !info.length
          ? '<span class="character-card-empty">暂无关系数据</span>'
          : ""
      }
    </div>`;
}
function renderWSCheckModifiers(modifiers) {
  const advantages = Array.isArray(modifiers?.advantages) ? modifiers.advantages : [];
  const disadvantages = Array.isArray(modifiers?.disadvantages)
    ? modifiers.disadvantages
    : [];
  const block = (title, icon, items, tone) => `
    <section class="mod-block ${tone}">
      <header><span>${icon}</span><strong>${title}</strong><span class="mod-count">${items.length}</span></header>
      <ul>${items.map((item) => `<li>${escapeHTML(String(item))}</li>`).join("") || "<li class='character-card-empty'>无</li>"}</ul>
    </section>`;
  return `
    <div class="mod-grid">
      ${block("优势", "✅", advantages, "mod-adv")}
      ${block("劣势", "⚠️", disadvantages, "mod-dis")}
    </div>`;
}
function wsScalarValue(value) {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value !== null && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value ?? "—");
}
function renderWSListCard(value) {
  const items = Array.isArray(value) ? value : [];
  if (!items.length) return '<span class="character-card-empty">无</span>';
  return `<div class="ws-chip-list">${items
    .map(
      (item) =>
        `<span class="ws-chip">${escapeHTML(wsScalarValue(item))}</span>`,
    )
    .join("")}</div>`;
}
function renderWSObjectCard(value) {
  const entries = Object.entries(value);
  const scalars = entries.filter(([, v]) => !v || typeof v !== "object");
  const nested = entries.filter(([, v]) => v && typeof v === "object");
  const tiles = scalars.length
    ? `<div class="ws-tile-grid">${scalars
        .map(
          ([key, v]) => `
            <div class="ws-tile">
              <span>${escapeHTML(key)}</span>
              <strong>${escapeHTML(wsScalarValue(v))}</strong>
            </div>`,
        )
        .join("")}</div>`
    : "";
  const nestedHtml = nested.length
    ? `<div class="ws-nested">${nested
        .map(
          ([key, v]) => `
            <div class="ws-nested-block">
              <header>${escapeHTML(key)}</header>
              <div class="ws-nested-body">
                ${renderWSObjectCard(v)}
              </div>
            </div>`,
        )
        .join("")}</div>`
    : "";
  return `<div class="ws-object">${tiles}${nestedHtml}</div>`;
}
function wsFieldMeta(key, value) {
  const icon =
    WS_FIELD_ICONS[key] ||
    (Array.isArray(value) ? "📋" : typeof value === "number" ? "🔢" : "📄");
  return {
    key,
    label: WS_FIELD_LABELS[key] || key,
    icon,
  };
}
function wsCard(key, value, idLabels) {
  if (key === "facts") return "";
  const meta = wsFieldMeta(key, value);
  let body = "";
  if (key === "progress" && value && typeof value === "object") {
    body = renderWSProgress(value);
  } else if (key === "inventory" && value && typeof value === "object") {
    body = renderWSInventory(value, idLabels);
  } else if (key === "relationships" && value && typeof value === "object") {
    body = renderWSRelationships(value, idLabels);
  } else if (key === "check_modifiers" && value && typeof value === "object") {
    body = renderWSCheckModifiers(value);
  } else if (Array.isArray(value)) {
    body = renderWSListCard(value);
  } else if (value !== null && typeof value === "object") {
    body = renderWSObjectCard(value);
  } else {
    body = `<div class="ws-scalar">${escapeHTML(wsScalarValue(value))}</div>`;
  }
  return `
    <article class="ws-card">
      <header class="ws-card-head">
        <span class="ws-card-icon">${meta.icon}</span>
        <div class="ws-card-title">
          <strong>${escapeHTML(meta.label)}</strong>
          <small>${escapeHTML(meta.key)}</small>
        </div>
      </header>
      <div class="ws-card-body">${body}</div>
    </article>`;
}
function renderWSFacts(facts) {
  const items = Array.isArray(facts) ? facts : [];
  if (!items.length) return "";
  // A12：已知事实改为可折叠组件，默认收起；点击标题展开/收起。
  return `
    <details class="ws-facts-panel">
      <summary class="ws-facts-head">
        <span class="ws-facts-toggle" aria-hidden="true">▸</span>
        <span aria-hidden="true">✦</span>
        <strong>已知事实</strong>
        <span class="ws-count">${items.length}</span>
      </summary>
      <div class="ws-facts">${items
        .map(
          (item) =>
            `<span class="ws-fact">${escapeHTML(wsScalarValue(item))}</span>`,
        )
        .join("")}</div>
    </details>`;
}
function renderWorldStateVisual(worldState, idLabels = {}) {
  const source =
    worldState && typeof worldState === "object" && !Array.isArray(worldState)
      ? worldState
      : {};
  const keys = Object.keys(source);
  // A23: 背包/物品栏与关系与好感度已迁至「小队列表/队伍概况」，
  // 不再在受控世界状态中重复展示（组织/势力关系并入队伍关系/声望）。
  const movedKeys = new Set(["inventory", "relationships"]);
  const ordered = [
    ...WS_FIELD_ORDER.filter(
      (k) => keys.includes(k) && !movedKeys.has(k),
    ),
    ...keys
      .filter((k) => !WS_FIELD_ORDER.includes(k) && !movedKeys.has(k))
      .sort(),
  ];
  const factsHtml = renderWSFacts(source.facts);
  const cardsHtml = ordered.map((key) => wsCard(key, source[key], idLabels)).join("");
  return `
    <div class="ws-visual">
      ${factsHtml}
      <div class="ws-card-grid">${cardsHtml}</div>
    </div>`;
}

function renderTimerList(timers) {
  if (!timers || !timers.length) {
    return `<div class="empty-state compact">暂无计时器</div>`;
  }
  return timers
    .map(
      (timer) => `
        <div class="integrity-item">
          <span>${escapeHTML(liveTimerLabel(timer.timer_type))}${
            timer.participant
              ? ` · ${escapeHTML(timer.participant)}`
              : ""
          }</span>
          <span class="status-badge status-${
            timer.status === "active" ? "running" : "paused"
          }">${escapeHTML(
            timer.status === "active"
              ? "进行中"
              : timer.status === "paused"
                ? "已暂停"
                : timer.status,
          )}</span>
        </div>`,
    )
    .join("");
}

// 0.12.0-A3（#3）：倒计时小窗口的轻量局部刷新——只请求并替换计时器列表，
// 避免整页重绘闪烁；排序由后端按 order 参数决定（desc=最新置顶 / asc=最紧迫）。
async function refreshLiveTimers() {
  const sessionId = $("#live-session-picker")?.value;
  const list = $("#timer-widget-list");
  if (!sessionId || !list) return;
  try {
    const resp = await bridge.apiGet("dashboard/timers", {
      session_id: sessionId,
      order: liveState.timerOrder,
    });
    list.innerHTML = renderTimerList(resp.timers || []);
    const hint = $(".widget-hint");
    if (hint) {
      hint.textContent = liveState.timerOrder === "desc" ? "最新置顶" : "最紧迫在上";
    }
  } catch (error) {
    // 局部刷新失败不打断仪表盘；保留上次渲染内容。
  }
}

// ── A20：LIVE 仪表盘可视化助手 ──────────────────────────────────────
function liveEntityIcon(target) {
  const t = String(target || "");
  if (t.startsWith("npc:") || t.startsWith("snpc") || /npc/i.test(t)) return "🧝";
  if (/队伍|party|team|小队|团队/.test(t)) return "🤝";
  if (/组织|势力|教会|公会|王国|商会|议会|守备队/.test(t)) return "🏛";
  if (t.startsWith("player:")) return "👤";
  return "👤";
}
function liveSourceLabel(source) {
  const s = String(source || "");
  if (s === "world_preset") return { icon: "🗺️", label: "世界预设" };
  if (s === "model_generated") return { icon: "✨", label: "动态生成" };
  if (s === "admin") return { icon: "🛠️", label: "人工创建" };
  return { icon: "❔", label: s || "未知" };
}
function liveOptionIcon(choice) {
  if (choice.requires_check) return "⚔️";
  if (choice.risk) return "⚠️";
  return "🎯";
}

function renderLiveDetail() {
  const detail = liveState.detail;
  if (!detail) return;
  const session = detail.session || {};
  const world = session.world || {};
  const world_state = session.world_state || {};
  const id_labels = detail.session?.id_labels || detail.id_labels || {};
  const turn = detail.turn || {};
  const timers = detail.timers || [];
  const choices = (detail.current_choice?.choices) || detail.active_choices || [];
  const vote = detail.active_vote || null;
  const squad = detail.squad || [];
  const npcs = detail.npcs || [];
  const ledger = detail.ledger || [];
  const clocks = detail.clocks || [];
  const partyRelations = detail.party_relations || [];
  const partyInventory = detail.party_inventory || [];
  const questItems = detail.quest_items || [];
  const economy = detail.economy || {};
  const events = detail.recent_events || [];
  const timeline = liveState.timeline || { events: [], operations: [] };
  const control = detail.control || { mode: "auto", active_dm_user_id: "" };
  const stateBadge = statusBadge(session.state || "closed");
  const activeTimers = timers.filter((item) => item.status === "active");
  const currentChoice = detail.current_choice || null;
  const isDm = control.mode === "dm";

  const esc = escapeHTML;
  const fmt = (value, fallback = "—") =>
    value === null || value === undefined || value === ""
      ? fallback
      : esc(String(value));
  const countActive = (squad || []).filter((m) =>
    ["active", "reserved"].includes(m.participation_status),
  ).length;

  // 小队卡：数值 / 状态 / 背包 / 关系（含图标与好感度可视化）
  const squadCards = squad
    .map((member, index) => {
      const delegated = (detail.delegations || []).some(
        (d) => d.owner_user_id === member.group_user_id,
      );
      const position = member.turn_position || 0;
      const hasChosen =
        currentChoice &&
        currentChoice.participant &&
        currentChoice.participant.group_user_id === member.group_user_id &&
        currentChoice.selected_key;
      const resources = member.resources || {};
      const resourceLabels = member.resource_labels || {};
      const resourceChips = Object.keys(resources)
        .map(
          (key) =>
            `<span class="squad-resource"><i>${esc(
              resourceLabels[key] || key,
            )}</i><b>${esc(String(resources[key]))}</b></span>`,
        )
        .join("");
      const invArr = Array.isArray(member.inventory) ? member.inventory : [];
      const invChips = invArr
        .slice(0, 6)
        .map((item) => {
          const label =
            typeof item === "string"
              ? item
              : item?.name || item?.label || JSON.stringify(item);
          const count =
            typeof item === "object" && item !== null && item.count
              ? ` ×${esc(String(item.count))}`
              : "";
          const icon =
            typeof item === "object" && item !== null
              ? wsItemIcon(item.name || "")
              : wsItemIcon(label);
          return `<span class="chip chip-item" title="${esc(
            item?.category || "",
          )}">${icon} ${esc(String(label))}${count}</span>`;
        })
        .join("");
      // A21: 关系目标解析为可读名（id_labels），好感度使用与「受控世界状态」
      // 一致的双向进度条（负=左侧红，正=右侧绿，从中间开始）。
      const memberFavValues = (member.relationships || []).flatMap((rel) => {
        const nums = rel.fields && typeof rel.fields === "object"
          ? Object.values(rel.fields).filter((v) => typeof v === "number")
          : [];
        return nums.length ? nums : (rel.favor != null ? [rel.favor] : []);
      });
      const memberFavRange = wsFavRange(memberFavValues.map((v) => ({ value: v })));
      const relRows = (member.relationships || [])
        .slice(0, 4)
        .map((rel) => {
          const targetMeta = wsTargetLabel(rel.target, id_labels);
          const targetLabel = targetMeta.label || fmt(rel.target);
          const numericFields = rel.fields && typeof rel.fields === "object"
            ? Object.entries(rel.fields).filter(([, v]) => typeof v === "number")
            : [];
          const favorBars = numericFields.length
            ? numericFields
                .map(([trait, v]) => wsFavBar(trait, v, memberFavRange))
                .join("")
            : rel.favor !== null && rel.favor !== undefined
              ? wsFavBar("好感", rel.favor, memberFavRange)
              : "";
          const extra = [
            favorBars,
            rel.stage ? `<span class="rel-stage">${esc(rel.stage)}</span>` : "",
            rel.summary ? `<span class="squad-rel-detail">${esc(rel.summary)}</span>` : "",
          ]
            .filter(Boolean)
            .join(" ");
          return `<div class="squad-rel">${liveEntityIcon(
            rel.target,
          )} <span class="rel-target">${esc(targetLabel)}</span> ${
            extra ? `<span class="squad-rel-extra">${extra}</span>` : ""
          }</div>`;
        })
        .join("");
      const badges = [
        member.is_current
          ? `<span class="squad-badge is-current">🎯 当前行动者</span>`
          : "",
        position ? `<span class="squad-badge">#${position}</span>` : "",
        hasChosen ? `<span class="squad-badge">✓ 已选择</span>` : "",
        delegated ? `<span class="squad-badge is-warn">🤝 托管中</span>` : "",
        member.ready ? `<span class="squad-badge">已准备</span>` : "",
        member.participation_status !== "active"
          ? `<span class="squad-badge is-muted">${esc(
              PARTICIPANT_STATUS_LABELS[member.participation_status] ||
                member.participation_status,
            )}</span>`
          : "",
      ]
        .filter(Boolean)
        .join("");
      return `<article class="squad-card ${
        member.is_current ? "is-current" : ""
      }">
        <div class="squad-card-head">
          <div class="squad-avatar">${esc(
            (member.character_name || member.display_name || "?").slice(0, 1),
          )}</div>
          <div class="squad-identity">
            <div class="squad-name">${fmt(member.character_name || member.display_name)}</div>
            <div class="squad-sub">${fmt(member.display_name)}${
              member.character_code ? ` · ${esc(member.character_code)}` : ""
            }${member.role ? ` · ${esc(member.role)}` : ""}</div>
          </div>
          <div class="squad-badges">${badges}</div>
        </div>
        <details class="squad-details" ${index === 0 ? "open" : ""}>
          <summary class="squad-summary">💠 数值 · ✨ 状态 · 🎒 背包 · 💗 关系</summary>
          <div class="squad-body">
            <div class="squad-block">
              <div class="squad-block-label">💠 当前数值</div>
              <div class="squad-resources">${
                resourceChips ||
                '<span class="character-card-empty">世界包未接入数值</span>'
              }</div>
            </div>
            <div class="squad-block">
              <div class="squad-block-label">✨ 当前状态</div>
              <div class="chip-row">${renderStatusChips(member.statuses)}</div>
            </div>
            <div class="squad-block">
              <div class="squad-block-label">🎒 随身物品</div>
              <div class="chip-row">${
                invChips ||
                '<span class="character-card-empty">空背包</span>'
              }</div>
            </div>
            <div class="squad-block">
              <div class="squad-block-label">💗 NPC 关系 / 好感度</div>
              ${
                relRows ||
                '<span class="character-card-empty">无关系记录</span>'
              }
            </div>
            <div class="squad-block">
              <div class="squad-block-label">📍 状态</div>
              <div class="chip-row">${
                member.current_location
                  ? `<span class="chip">📍 ${esc(member.current_location)}</span>`
                  : ""
              }${
                member.reputation
                  ? `<span class="chip">🎖 声望 ${esc(member.reputation)}</span>`
                  : ""
              }<span class="chip">${
                member.ready ? "已准备" : "未准备"
              }</span></div>
            </div>
          </div>
        </details>
      </article>`;
    })
    .join("");

  // NPC 卡片（来源 / 出场轮次可视化）
  const npcCards = npcs
    .map((npc) => {
      const source = liveSourceLabel(npc.source);
      return `
        <article class="npc-card">
          <div class="npc-card-head">
            <span class="npc-avatar">${esc((npc.name || "?").slice(0, 1))}</span>
            <div>
              <div class="npc-name">${fmt(npc.name)}</div>
              <div class="npc-sub">${fmt(npc.role_type || npc.identity || "NPC")}${
                npc.organization ? ` · ${esc(npc.organization)}` : ""
              }</div>
            </div>
          </div>
          <div class="npc-meta">
            <span class="npc-meta-badge ${
              npc.lifecycle_status === "active" ? "is-on" : ""
            }">${
              npc.lifecycle_status === "active" ? "●" : "○"
            } ${
              npc.lifecycle_status === "active"
                ? "在场"
                : esc(npc.lifecycle_status || "离场")
            }</span>
            <span class="npc-meta-badge">📍 ${fmt(npc.location || "位置未知")}</span>
            <span class="npc-meta-badge">${fmt(npc.status || "状态正常")}</span>
          </div>
          <div class="npc-foot">
            <span class="npc-meta-badge" title="来源">${source.icon} ${esc(
              source.label,
            )}</span>
            <span class="npc-meta-badge" title="最近出场轮次">🎬 最近出场第 ${fmt(
              npc.last_turn || "—",
            )} 轮</span>
          </div>
        </article>`;
    })
    .join("");

  // 剧情账本 + 场景时钟
  const ledgerRows = ledger
    .map(
      (item) => `
        <div class="ledger-row">
          <span class="ledger-icon">📜</span>
          <div class="ledger-main">
            <div class="ledger-title">${fmt(item.title)}</div>
            <div class="ledger-meta">${fmt(item.kind)} · ${fmt(
              item.description,
            )}</div>
          </div>
          <span class="status-badge">${fmt(item.status)}</span>
        </div>`,
    )
    .join("");
  const clockRows = clocks
    .map(
      (item) => `
        <div class="ledger-row">
          <span class="ledger-icon">⏳</span>
          <div class="ledger-main">
            <div class="ledger-title">${fmt(item.title)}</div>
            <div class="ledger-meta">${fmt(item.visibility)} · ${fmt(
              item.status,
            )}</div>
          </div>
          <strong>${fmt(item.current_value)}/${fmt(item.segments)}</strong>
        </div>`,
    )
    .join("");

  // 队伍关系（来源→目标 解析；A21：目标名解析 + 双向好感度条）
  const partyFavValues = partyRelations.flatMap((rel) => {
    const nums = rel.fields && typeof rel.fields === "object"
      ? Object.values(rel.fields).filter((v) => typeof v === "number")
      : [];
    return nums.length ? nums : (rel.favor != null ? [rel.favor] : []);
  });
  const partyFavRange = wsFavRange(partyFavValues.map((v) => ({ value: v })));
  const partyRows = partyRelations
    .map((rel) => {
      const meta = wsTargetLabel(rel.label, id_labels);
      const label = meta.label || fmt(rel.label);
      // A23: 组织/势力关系（字符串描述）与队伍数值关系分开展示。
      if (rel.kind === "info") {
        return `
          <div class="party-rel-item">
            <div class="vote-row">
              <span class="vote-key">🏛</span>
              <span class="vote-text">${esc(label)}</span>
            </div>
            <div class="party-rel-bars"><span class="party-rel-summary">${esc(
              rel.summary || "",
            )}</span></div>
          </div>`;
      }
      const numericFields = rel.fields && typeof rel.fields === "object"
        ? Object.entries(rel.fields).filter(([, v]) => typeof v === "number")
        : [];
      const favorBars = numericFields.length
        ? numericFields
            .map(([trait, v]) => wsFavBar(trait, v, partyFavRange))
            .join("")
        : rel.favor !== undefined && rel.favor !== null
          ? wsFavBar("好感", rel.favor, partyFavRange)
          : "";
      return `
        <div class="party-rel-item">
          <div class="vote-row">
            <span class="vote-key">${liveEntityIcon(rel.label)}</span>
            <span class="vote-text">${esc(label)}</span>
          </div>
          <div class="party-rel-bars">${
            favorBars ||
            (rel.summary
              ? `<span class="party-rel-summary">${esc(rel.summary)}</span>`
              : "—")
          }</div>
        </div>`;
    })
    .join("");

  // 队伍物资 / 任务物品
  const renderInvRows = (items, icon) =>
    (items || [])
      .slice(0, 8)
      .map(
        (item) => `
          <div class="vote-row">
            <span class="vote-key">${wsItemIcon(item.name || "")}</span>
            <span class="vote-text">${fmt(item.name)}${
              item.category ? ` <small>· ${esc(item.category)}</small>` : ""
            }</span>
            <span class="vote-count">×${fmt(item.count)}</span>
          </div>`,
      )
      .join("");
  const partyInvRows = renderInvRows(partyInventory, "🎒");
  const questItemRows = renderInvRows(questItems, "🗝️");

  // 经济摘要
  const economyEnabled = Boolean(economy.enabled);
  const walletRows = (economy.wallets || [])
    .slice(0, 8)
    .map(
      (wallet) => `
        <div class="vote-row">
          <span class="vote-key">💱</span>
          <span class="vote-text">${fmt(wallet.owner_type)} · ${fmt(
            wallet.currency_id,
          )}</span>
          <span class="vote-count">${fmt(wallet.balance)}</span>
        </div>`,
    )
    .join("");

  const voteMeta = vote
    ? `<div class="field-hint" style="margin-bottom:8px">🗳 已投 ${
        vote.voters ?? 0
      }/${(vote.eligible_user_ids || []).length} 人 · 未投 ${
        (vote.unvoted_user_ids || []).length
      } 人${
        vote.remaining_seconds
          ? " · ⏳ 剩余 " + Math.ceil(vote.remaining_seconds / 60) + " 分钟"
          : ""
      }</div>`
    : "";
  const voteRows = (vote?.options || [])
    .map(
      (option) => `
        <div class="vote-row">
          <span class="vote-key">${esc(option.key || "?")}</span>
          <span class="vote-text">${esc(option.text || "")}</span>
          <span class="vote-count">${option.votes ?? 0} 票</span>
          <button class="action-button" data-live-vote="${esc(
            option.key || "",
          )}">代投</button>
        </div>`,
    )
    .join("");
  const choiceRows = choices
    .map(
      (choice) => `
        <div class="vote-row">
          <span class="vote-key">${liveOptionIcon(choice)}</span>
          <span class="vote-text">${esc(choice.key || "?")}. ${esc(
            choice.text || "",
          )}${choice.requires_check ? " · ⚔ 需检定" : ""}${
            choice.risk ? ` · ⚠ 风险 ${esc(choice.risk)}` : ""
          }</span>
          <button class="action-button" data-live-force="${esc(
            choice.key || "",
          )}">代选</button>
        </div>`,
    )
    .join("");

  const eventRows = (timeline.events || [])
    .map(
      (event) => `
        <li class="timeline-row">
          <span class="timeline-role">${esc(event.role || "—")}</span>
          <span class="timeline-content">${esc(event.content || "")}</span>
          <time class="timeline-time">${esc(formatDate(event.created_at))}</time>
        </li>`,
    )
    .join("");

  const timerPanel = `
    <article class="panel">
      <div class="panel-head">
        <div>
          <div class="eyebrow">TIMERS</div>
          <h2>⏱ 倒计时 <span class="widget-hint">最新置顶</span></h2>
        </div>
        <button class="action-button" id="timer-order-toggle"
          type="button" title="切换排序：最新置顶 / 最紧迫在上">↕ 切换排序</button>
      </div>
      <div class="timer-widget">
        <div class="integrity-list" id="timer-widget-list">
          ${renderTimerList(timers)}
        </div>
      </div>
    </article>`;

  $("#live-detail-root").innerHTML = `
    <div class="live-dashboard">
      <!-- 第一层：副本状态概览 -->
      <div class="metric-grid live-metric-grid">
        <article class="metric-card">
          <div class="metric-label">◉ 副本状态</div>
          <div class="metric-value">${stateBadge}</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">🗺 世界</div>
          <div class="metric-value metric-small">${fmt(world.name)}</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">🔁 回合</div>
          <div class="metric-value">第 ${fmt(turn.round_no || session.turn_no || 1)} 轮</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">🎯 当前行动者</div>
          <div class="metric-value metric-small">${fmt(turn.current_name)}</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">👥 在场角色</div>
          <div class="metric-value">${countActive} 人</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">主持模式</div>
          <div class="metric-value metric-small">${isDm ? "🎮 人工 DM" : "🤖 AI 自动"}</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">🔒 输入锁</div>
          <div class="metric-value">${session.input_locked ? "已锁定" : "未锁定"}</div>
        </article>
        <article class="metric-card">
          <div class="metric-label">⏱ 活跃计时器</div>
          <div class="metric-value">${activeTimers.length}</div>
        </article>
      </div>

      <!-- 第二层：人工 DM 控制台（独立标题 + 总开关 + 可折叠） -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🎛</span>
          <h2>人工 DM 控制台</h2>
          <button class="action-button ${isDm ? "is-danger" : ""}" data-session-detail-action="dm-master-toggle" data-dm-enabled="${isDm ? "1" : "0"}">${
            isDm ? "关闭人工 DM" : "开启人工 DM"
          }</button>
          <span class="status-badge ${isDm ? "status-running" : ""}">${
            isDm ? "🎮 已激活" : "🤖 未激活"
          }</span>
        </header>
        <details class="panel" ${isDm ? "open" : ""}>
          <summary class="tb-section-head" style="cursor:pointer;padding:var(--sp-3) 0">${
            isDm ? "▼ 控制功能（点击折叠）" : "控制功能未激活（点击展开查看）"
          }</summary>
          <div style="padding-top:8px">${renderDMConsole(detail)}</div>
        </details>
      </section>

      <!-- 第三层：回合与行动者 -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🎯</span>
          <h2>回合与行动者</h2>
          <span class="status-badge">第 ${fmt(turn.round_no || 1)} 轮</span>
        </header>
        <div class="live-turn-bar">
          <div class="turn-order-list turn-order-inline">
            ${(turn.order || [])
              .map(
                (item) => `
                  <div class="turn-order-row ${
                    item.user_id === turn.current_user_id ? "is-current" : ""
                  }">
                    <span class="turn-position">${item.user_id === turn.current_user_id ? "🎯" : fmt(item.position)}</span>
                    <span class="turn-name">${fmt(item.name)}</span>
                    <span class="turn-actions">
                      <button class="icon-btn" data-live-turn="up" data-user-id="${esc(
                        item.user_id || "",
                      )}" title="上移">↑</button>
                      <button class="icon-btn" data-live-turn="down" data-user-id="${esc(
                        item.user_id || "",
                      )}" title="下移">↓</button>
                      <button class="action-button" data-live-turn="designate" data-user-id="${esc(
                        item.user_id || "",
                      )}">指定</button>
                      <button class="action-button" data-live-turn="skip" data-user-id="${esc(
                        item.user_id || "",
                      )}">⏭ 跳过</button>
                    </span>
                  </div>`,
              )
              .join("") ||
              `<div class="empty-state compact">尚无回合秩序</div>`}
          </div>
          <div class="live-turn-actions">
            <button class="action-button is-danger" data-live-turn="supersede">作废当前选项</button>
          </div>
        </div>
      </section>

      <!-- 第四层：小队列表 -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🧑‍🤝‍🧑</span>
          <h2>小队列表</h2>
          <span class="status-badge">${squad.length} 名成员</span>
        </header>
        <div class="squad-grid">${
          squadCards ||
          '<div class="empty-state compact">当前没有玩家角色</div>'
        }</div>
      </section>

      <!-- 第五层：队伍概况（关系 / 物资 / 任务物品 / 资金） -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🤝</span>
          <h2>队伍概况</h2>
          <span class="status-badge">关系 ${partyRelations.length} · 物资 ${partyInventory.length} · 任务 ${questItems.length}</span>
        </header>
        <div class="live-col-2">
          <article class="panel">
            <div class="panel-head"><div><div class="eyebrow">PARTY RELATIONS</div><h2>🤝 队伍关系 / 声望</h2></div></div>
            <div class="vote-list">${
              partyRows ||
              '<div class="empty-state compact">暂无队伍级关系数据</div>'
            }</div>
          </article>
          <article class="panel">
            <div class="panel-head"><div><div class="eyebrow">PARTY SUPPLIES</div><h2>🎒 队伍物资</h2></div></div>
            <div class="vote-list">${
              partyInvRows ||
              '<div class="empty-state compact">暂无队伍物资</div>'
            }</div>
          </article>
          <article class="panel">
            <div class="panel-head"><div><div class="eyebrow">QUEST ITEMS</div><h2>🗝️ 任务物品</h2></div></div>
            <div class="vote-list">${
              questItemRows ||
              '<div class="empty-state compact">暂无任务物品</div>'
            }</div>
          </article>
          ${timerPanel}
        </div>
        ${
          economyEnabled
            ? `<article class="panel" style="margin-top:14px">
                <div class="panel-head"><div><div class="eyebrow">ECONOMY</div><h2>💱 经济信息</h2></div>
                <span class="status-badge">已启用</span></div>
                <div class="vote-list">${
                  walletRows ||
                  '<div class="empty-state compact">暂无钱包数据</div>'
                }</div>
              </article>`
            : ""
        }
      </section>

      <!-- 第六层：行动选项与表决 -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🗳️</span>
          <h2>行动选项与表决</h2>
        </header>
        <div class="live-col-2">
          <article class="panel">
            <div class="panel-head">
              <div>
                <div class="eyebrow">ACTIVE CHOICE</div>
                <h2>🎯 活跃选项</h2>
              </div>
              <span class="status-badge">${
                currentChoice?.participant?.character_name ||
                currentChoice?.participant?.display_name ||
                "无"
              }</span>
            </div>
            ${
              currentChoice?.flavor_text
                ? `<div class="storyline-card"><div class="storyline-label">📜 当前故事线</div><div class="storyline-text">${esc(
                    currentChoice.flavor_text,
                  )}</div></div>`
                : ""
            }
            <div class="vote-list">${
              choiceRows ||
              '<div class="empty-state compact">暂无活跃选项</div>'
            }</div>
            ${
              currentChoice
                ? `<p class="field-hint" style="margin-top:8px">已选：${
                    currentChoice.selected_key
                      ? esc(currentChoice.selected_key)
                      : "未选择"
                  } · 重整 ${fmt(currentChoice.reroll_count)} / 1</p>`
                : ""
            }
          </article>
          <article class="panel">
            <div class="panel-head">
              <div>
                <div class="eyebrow">GROUP VOTE</div>
                <h2>🗳 集体表决 ${vote ? "" : "（无进行中表决）"}</h2>
              </div>
            </div>
            ${
              vote
                ? `<div class="vote-list">${voteMeta}${voteRows}</div>`
                : `<div class="empty-state compact">当前没有进行中的集体表决</div>`
            }
          </article>
        </div>
        ${renderDelegationPanel(detail)}
        ${
          liveState.lastForcedResult
            ? `<details class="panel forced-result-card" style="margin-top:14px">
                <summary class="tb-section-head" style="cursor:pointer;padding:var(--sp-2) 0">🎭 最近后台代选结果（${esc(
                  liveState.lastForcedResult.at || "",
                )}）· 点击展开</summary>
                <div class="forced-result-body">${
                  liveState.lastForcedResult.story
                    ? `<div class="forced-result-story">${esc(
                        liveState.lastForcedResult.story,
                      )}</div>`
                    : ""
                }${
                  liveState.lastForcedResult.turn
                    ? `<div class="forced-result-turn">${esc(
                        liveState.lastForcedResult.turn,
                      )}</div>`
                    : ""
                }</div>
              </details>`
            : ""
        }
        ${
          (detail.return_requests || []).length
            ? `<div class="tab-card" style="margin-top:14px">
                <div class="tab-card-head"><span class="row-icon" aria-hidden="true">🛬</span><strong>返场申请</strong></div>
                <div class="session-stack">
                  ${(detail.return_requests || [])
                    .map(
                      (item) => `<div class="session-row">
                        <span class="row-icon" aria-hidden="true">🛬</span><div><div class="session-name">
                        ${esc(item.character_name || item.display_name || "—")}</div>
                        <div class="session-meta">${esc(item.objective || "")}</div></div>
                        <div class="session-location">${esc(item.status || "—")}</div></div>`,
                    )
                    .join("")}
                </div>
              </div>`
            : ""
        }
      </section>

      <!-- 第七层：剧情进度与时间 -->
      <section class="live-section">
        <header class="live-section-head">
          <span>📜</span>
          <h2>剧情进度与时间</h2>
          <span class="status-badge">账本 ${ledger.length} · 时钟 ${clocks.length}</span>
        </header>
        <div class="live-col-2">
          <article class="panel">
            <div class="panel-head"><div><div class="eyebrow">STORY LEDGER</div><h2>📜 任务与剧情线账本</h2></div></div>
            <div class="session-stack">${
              ledgerRows ||
              '<div class="empty-state compact">尚无剧情账本条目</div>'
            }</div>
          </article>
          <article class="panel">
            <div class="panel-head"><div><div class="eyebrow">SCENE CLOCKS</div><h2>⏳ 场景时钟</h2></div></div>
            <div class="session-stack">${
              clockRows ||
              '<div class="empty-state compact">尚无场景时钟</div>'
            }</div>
          </article>
        </div>
      </section>

      <!-- 第八层：NPC 信息 -->
      <section class="live-section">
        <header class="live-section-head">
          <span>🧝</span>
          <h2>NPC 信息</h2>
          <span class="status-badge">${npcs.length} 名</span>
        </header>
        <div class="npc-grid">${
          npcCards ||
          '<div class="empty-state compact">当前副本没有 NPC</div>'
        }</div>
      </section>

      <!-- 第九层：受控世界状态 -->
      <article class="panel panel-span-2" style="margin:18px 0">
        <div class="panel-head">
          <div>
            <div class="eyebrow">CONTROLLED WORLD STATE</div>
            <h2>🌍 受控世界状态</h2>
          </div>
          <span class="status-badge">修订 r${fmt(session.revision)}</span>
        </div>
        <div class="world-state-card">
          ${renderWorldStateVisual(world_state, id_labels)}
        </div>
      </article>

      <!-- 第十层：其他辅助信息 -->
      <div class="live-col-2">
        <article class="panel">
          <div class="panel-head">
            <div>
              <div class="eyebrow">TIMELINE</div>
              <h2>📜 事件时间线（回放）</h2>
            </div>
            <span class="status-badge">${timeline.events.length} 条事件</span>
          </div>
          <ul class="timeline-list">${
            eventRows || '<li class="timeline-row">暂无事件</li>'
          }</ul>
        </article>
        <article class="panel">
          <div class="panel-head"><div><div class="eyebrow">RECENT EVENTS</div><h2>✨ 最近生成事件</h2></div></div>
          <div class="integrity-list">
            ${events
              .map(
                (event) => `
                  <div class="integrity-item">
                    <span class="timeline-role">${esc(event.role || "—")}</span>
                    <span>${esc(event.content || "")}</span>
                  </div>`,
              )
              .join("") ||
              '<div class="empty-state compact">暂无事件</div>'}
          </div>
        </article>
      </div>
    </div>`;
}

// ── v0.12.0：世界包社区注册表 / 市场 ─────────────────────────────────
const marketState = {
  items: [],
  query: "",
  manifestUrl: "",
  remoteLoaded: false,
};

async function loadMarket() {
  const resp = await bridge.apiGet("market/list", {
    q: marketState.query,
    manifest_url: marketState.manifestUrl || undefined,
  });
  marketState.items = resp.items || [];
  marketState.remoteLoaded = Boolean(resp.remote_enabled);
  renderMarket();
}

// 0.12.0-A4：GitHub 直链清单拉取（不落配置，本次会话内生效）。
async function fetchRemoteMarket() {
  const input = $("#market-url-input");
  const url = String(input?.value || "").trim();
  if (!url) {
    showError(new Error("请先填写 GitHub 直链清单地址"));
    return;
  }
  marketState.manifestUrl = url;
  await withBusy($("#market-url-fetch"), loadMarket);
  const remoteClear = $("#market-url-clear");
  if (remoteClear) remoteClear.hidden = false;
}

function clearRemoteMarket() {
  marketState.manifestUrl = "";
  marketState.remoteLoaded = false;
  const input = $("#market-url-input");
  if (input) input.value = "";
  const remoteClear = $("#market-url-clear");
  if (remoteClear) remoteClear.hidden = true;
  loadMarket().catch(showError);
}

function renderMarket() {
  const grid = $("#market-card-grid");
  $("#market-result-count").textContent = `共 ${marketState.items.length} 个世界包${
    marketState.remoteLoaded ? " · 远程已加载" : ""
  }`;
  if (!marketState.items.length) {
    grid.innerHTML = `
      <div class="empty-state compact market-empty">
        <div class="empty-symbol">▦</div>
        <span>市场默认为空：填入上方 GitHub 直链清单地址并点击「拉取远程」加载世界包</span>
      </div>`;
    return;
  }
  grid.innerHTML = marketState.items
    .map((item) => {
      const tags = (item.tags || [])
        .map(
          (tag) =>
            `<span class="status-badge status-running">${escapeHTML(
              tag,
            )}</span>`,
        )
        .join(" ");
      return `
        <article class="market-card">
          <div class="market-card-head">
            <h3>${escapeHTML(item.name || item.slug)}</h3>
            <span class="status-badge">v${escapeHTML(
              String(item.schema_version || "?"),
            )}</span>
          </div>
          <p class="market-desc">${escapeHTML(item.description || "（无描述）")}</p>
          <div class="market-meta">
            <code>${escapeHTML(item.slug || "")}</code>
            <span>${escapeHTML(item.source || "")}${
              item.source === "remote"
                ? '<span class="status-badge status-running">远程</span>'
                : ""
            }</span>
            <span>${formatBytes(item.size_bytes)}</span>
            ${tags}
          </div>
          <div class="market-actions">
            <button class="action-button" type="button"
              data-market-preview="${escapeHTML(item.package_key)}">
              预览 / 导入
            </button>
          </div>
        </article>`;
    })
    .join("");
}

async function previewMarketItem(packageKey) {
  const resp = await bridge.apiPost("market/fetch", {
    package_key: packageKey,
  });
  const entry = resp.entry || {};
  const report = resp.preflight || {};
  const issues = (report.issues || [])
    .map((issue) => `<li>${escapeHTML(String(issue.message || ""))}</li>`)
    .join("");
  const world = resp.world || {};
  openEditor({
    title: entry.name || entry.slug || "世界包预览",
    kicker: "MARKET · WORLD PACKAGE",
    body: `
      <p class="field-hint">${escapeHTML(
        entry.description || "（无描述）",
      )}</p>
      <div class="market-meta">
        <code>${escapeHTML(entry.slug || "")}</code>
        <span>协议 v${escapeHTML(String(entry.schema_version || "?"))}</span>
        <span>内容版本 ${escapeHTML(entry.content_version || "—")}</span>
        <span>来源 ${escapeHTML(entry.source || "—")}</span>
      </div>
      <details>
        <summary>世界体检报告（${report.compatible ? "通过" : "存在问题"}）</summary>
        <ul class="issue-list">${issues || "<li>无异常</li>"}</ul>
      </details>
      <details>
        <summary>开场场景预览</summary>
        <pre class="json-preview">${escapeHTML(
          String(world.opening_scene || "")).slice(0, 800)}</pre>
      </details>`,
    saveLabel: "导入世界包",
    onSave: async () => {
      await submitWorldImport(world, "auto");
      closeEditor();
    },
  });
}

function bindV0120() {
  $("#live-session-picker").addEventListener("change", () => {
    refreshLiveDetail().catch(showError);
  });
  $("#live-refresh-button").addEventListener("click", async (event) => {
    await withBusy(event.currentTarget, refreshLiveDetail);
  });
  // 0.12.0-A3（#3）：倒计时排序切换（按钮随渲染重建，用事件委托）。
  $("#live-detail-root").addEventListener("click", (event) => {
    const toggle = event.target.closest("#timer-order-toggle");
    if (!toggle) return;
    liveState.timerOrder = liveState.timerOrder === "desc" ? "asc" : "desc";
    refreshLiveTimers();
  });
  // A17：LIVE 回合/选项/投票后台操作（事件委托，随每次渲染重建）。
  $("#live-detail-root").addEventListener("click", async (event) => {
    const turnBtn = event.target.closest("[data-live-turn]");
    if (turnBtn) {
      const cmd = turnBtn.dataset.liveTurn;
      const sid = $("#live-session-picker")?.value;
      if (!sid) return;
      try {
        if (cmd === "supersede") {
          const ok = await confirmAction(
            "作废当前活跃选项？",
            "作废后当前 A–D 选项失效，可按需重新生成。",
            "确认作废",
          );
          if (!ok) return;
          await bridge.apiPost("sessions/turn-command", {
            session_id: sid,
            command: "supersede_choices",
          });
        } else if (cmd === "designate") {
          await bridge.apiPost("sessions/turn-command", {
            session_id: sid,
            command: "designate",
            user_id: turnBtn.dataset.userId,
          });
        } else if (cmd === "skip") {
          await bridge.apiPost("sessions/turn-command", {
            session_id: sid,
            command: "skip",
            user_id: turnBtn.dataset.userId,
          });
        } else if (cmd === "up" || cmd === "down") {
          const detail = liveState.detail || {};
          const order = (detail.turn?.order || []).map((x) => x.user_id);
          const idx = order.indexOf(turnBtn.dataset.userId);
          const target = cmd === "up" ? idx - 1 : idx + 1;
          if (idx < 0 || target < 0 || target >= order.length) return;
          [order[idx], order[target]] = [order[target], order[idx]];
          await bridge.apiPost("sessions/turn-command", {
            session_id: sid,
            command: "reorder",
            order,
          });
        }
        toast("回合操作已生效", "success");
        await refreshLiveDetail();
      } catch (error) {
        showError(error);
      }
      return;
    }
    const forceBtn = event.target.closest("[data-live-force]");
    if (forceBtn) {
      const sid = $("#live-session-picker")?.value;
      if (!sid) return;
      const ok = await confirmAction(
        "后台代选？",
        `将为当前行动角色选择 ${forceBtn.dataset.liveForce}，并触发完整选择流程与群聊通知。`,
        "确认代选",
      );
      if (!ok) return;
      try {
        const resp = await bridge.apiPost("delegations/forced-choose", {
          session_id: sid,
          choice_key: forceBtn.dataset.liveForce,
          operation_id: `forced:${Date.now()}`,
        });
        if (resp?.ok === false) {
          toast(resp.message || "代选未完成", "error");
        } else if (resp?.notice_sent) {
          toast("已代选并通知群聊", "success");
        } else {
          toast("操作已提交，但群聊通知发送失败：" + (resp?.notice_reason || "未知原因"), "warn");
        }
        if (resp?.ok !== false && (resp?.story || resp?.turn)) {
          liveState.lastForcedResult = {
            story: resp.story || "",
            turn: resp.turn || "",
            at: new Date().toLocaleString(),
          };
        }
        await refreshLiveDetail();
      } catch (error) {
        showError(error);
      }
      return;
    }
    const voteBtn = event.target.closest("[data-live-vote]");
    if (voteBtn) {
      const sid = $("#live-session-picker")?.value;
      if (!sid) return;
      const target = await promptForText({
        title: "代投",
        kicker: "VOTE AS",
        label: "替谁投票（该角色拥有者的真实平台用户 ID）",
        placeholder: "例如：123456789",
        required: true,
      });
      if (!target) return;
      try {
        await bridge.apiPost("dm/command", {
          session_id: sid,
          command: "vote_as",
          user_id: target.trim(),
          key: voteBtn.dataset.liveVote,
        });
        toast("已代投", "success");
        await refreshLiveDetail();
      } catch (error) {
        showError(error);
      }
      return;
    }
  });

  $("#live-seed-quota-button").addEventListener("click", async (event) => {
    const sessionId = $("#live-session-picker").value;
    if (!sessionId) return;
    await withBusy(event.currentTarget, async () => {
      const resp = await bridge.apiPost("dashboard/seed-quota", {
        session_id: sessionId,
      });
      toast(
        resp.seeded ? "已按配置默认值播种配额" : "配置未启用默认配额，未作改动",
        resp.seeded ? "success" : "info",
      );
      await refreshLiveDetail();
    });
  });
  $("#market-search").addEventListener("input", (event) => {
    marketState.query = event.target.value.trim();
    loadMarket().catch(showError);
  });
  $("#market-search-clear").addEventListener("click", () => {
    $("#market-search").value = "";
    marketState.query = "";
    loadMarket().catch(showError);
  });
  // 0.12.0-A4：GitHub 直链清单拉取。
  $("#market-url-fetch").addEventListener("click", () => {
    fetchRemoteMarket().catch(showError);
  });
  $("#market-url-clear").addEventListener("click", clearRemoteMarket);
  $("#market-url-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      fetchRemoteMarket().catch(showError);
    }
  });
  $("#market-card-grid").addEventListener("click", (event) => {
    const button = event.target.closest("[data-market-preview]");
    if (!button) return;
    previewMarketItem(button.dataset.marketPreview).catch(showError);
  });
}

// ── 0.12.0-A3（#1）：WebUI 可视化世界创建向导 ────────────────────────
const WIZARD_DRAFT_KEY = "tavern_world_wizard_draft_v2";
const WIZARD_DEFAULT_PROMPT =
  "你是酒馆叙事裁定器，负责主持一个多人跑团故事。" +
  "不替玩家决定行动与台词，玩家只能声明尝试；已发生事实必须连续；" +
  "信息不足时保守裁定，结尾为下一位玩家留出可回应的局面。";

const wizardState = {
  step: 1,
  draft: {
    name: "",
    slug: "",
    description: "",
    system_prompt: WIZARD_DEFAULT_PROMPT,
    resolution_mode: "attribute",
    attributes: [{ key: "", label: "" }],
    dice_system: "d20",
    default_difficulty: 12,
    difficulty_min: 5,
    difficulty_max: 25,
    recommended_min: 2,
    recommended_max: 4,
    minimum_start: 2,
    maximum: 4,
    allow_player_result_claims: false,
    strict_choices: true,
    opening_scene: "",
    location: "",
    time: "",
    scene_summary: "",
    facts: "",
  },
  preflight: null,
};

const WIZARD_STEPS = [
  ["基本信息", "name 描述 系统提示词"],
  ["数值与检定", "检定模式 属性 骰制"],
  ["规则与难度", "DC 区间 席位 严格选项"],
  ["开场与初始状态", "开场场景 地点 事实"],
  ["体检与发布", "预检报告 导入"],
];

function slugify(value) {
  const ascii = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return ascii || "new-world";
}

function loadWizardDraft() {
  try {
    const raw = safeLocalStorage()?.getItem(WIZARD_DRAFT_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      wizardState.draft = { ...wizardState.draft, ...saved };
    }
  } catch (error) {
    // 草稿损坏时忽略，从默认开始。
  }
}

function saveWizardDraft() {
  try {
    safeLocalStorage()?.setItem(WIZARD_DRAFT_KEY, JSON.stringify(wizardState.draft));
    const hint = $("#wizard-draft-hint");
    if (hint) hint.textContent = "草稿已自动保存在本浏览器";
  } catch (error) {
    // 本地存储不可用时静默跳过。
  }
}

function clearWizardDraft() {
  try {
    safeLocalStorage()?.removeItem(WIZARD_DRAFT_KEY);
  } catch (error) {
    // 忽略
  }
}

function wizardDraftToWorld() {
  const draft = wizardState.draft;
  const attributes = (draft.attributes || [])
    .map((item) => ({
      key: String(item.key || "").trim(),
      label: String(item.label || "").trim(),
    }))
    .filter((item) => item.key && item.label);
  const facts = String(draft.facts || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const resolution = {
    mode: draft.resolution_mode === "dice_only" ? "dice_only" : "attribute",
    dice_system: String(draft.dice_system || "d20").trim() || "d20",
    unknown_attribute: "reject",
    difficulty_policy: {
      controlled: null,
      dangerous: null,
      desperate: Number(draft.default_difficulty) || 12,
    },
    outcome_policy: {
      natural_20_critical: true,
      natural_1_critical: true,
      critical_success_margin: 10,
      critical_failure_margin: 10,
    },
  };
  if (resolution.mode === "attribute") {
    resolution.allowed_attributes = attributes.map((item) => item.key);
    resolution.generic_check = { enabled: false, label: "通用", modifier: 0 };
    resolution.option_checks = { enabled: true };
  }
  const characterCard = {
    template_version: 6,
    required_keys: ["name"],
    attribute_keys: attributes.map((item) => item.key),
  };
  return {
    slug: slugify(draft.slug || draft.name),
    name: String(draft.name || "").trim() || "未命名世界",
    description: String(draft.description || "").trim(),
    system_prompt: String(draft.system_prompt || WIZARD_DEFAULT_PROMPT).trim(),
    opening_scene: String(draft.opening_scene || "").trim(),
    world_schema_version: 5,
    world_content_version: "1.0.0",
    minimum_plugin_version: "0.12.0",
    rules: {
      resolution,
      strict_choices: Boolean(draft.strict_choices),
      allow_player_result_claims: Boolean(
        draft.allow_player_result_claims,
      ),
      player_limits: {
        recommended_min: Number(draft.recommended_min) || 2,
        recommended_max: Number(draft.recommended_max) || 4,
        minimum_start: Number(draft.minimum_start) || 2,
        maximum: Number(draft.maximum) || 4,
      },
      character_card: characterCard,
    },
    initial_state: {
      location: String(draft.location || "").trim(),
      time: String(draft.time || "").trim(),
      scene_summary: String(draft.scene_summary || "").trim(),
      facts,
    },
    protocol: { core_version: 5, features: {} },
    required_features: [],
  };
}

function wizardStepMarkup(step) {
  const draft = wizardState.draft;
  if (step === 1) {
    return `
      <label class="field-label">世界名称 *</label>
      <input class="wizard-input" data-wizard="name" value="${escapeHTML(
        draft.name || "",
      )}" placeholder="如：雾屿历险记" />
      <label class="field-label">世界标识（slug）</label>
      <input class="wizard-input" data-wizard="slug" value="${escapeHTML(
        draft.slug || "",
      )}" placeholder="留空自动从名称生成（如 wuyu-lixianji）" />
      <label class="field-label">一句话描述</label>
      <input class="wizard-input" data-wizard="description" value="${escapeHTML(
        draft.description || "",
      )}" placeholder="这个世界讲什么？" />
      <label class="field-label">系统提示词（给叙事模型）</label>
      <textarea class="wizard-textarea" data-wizard="system_prompt" rows="5">${escapeHTML(
        draft.system_prompt || WIZARD_DEFAULT_PROMPT,
      )}</textarea>
      <p class="field-hint">协议版本固定为 v5；内容版本从 1.0.0 起步，导入后可在世界编辑中调整。</p>`;
  }
  if (step === 2) {
    const attrRows = (draft.attributes || [{ key: "", label: "" }])
      .map(
        (item, index) => `
        <div class="wizard-attr-row">
          <input class="wizard-input wizard-attr-key" data-wizard-attr="key"
            data-index="${index}" value="${escapeHTML(item.key || "")}"
            placeholder="属性 ID（如 body）" />
          <input class="wizard-input wizard-attr-label" data-wizard-attr="label"
            data-index="${index}" value="${escapeHTML(item.label || "")}"
            placeholder="显示名（如 体魄）" />
          <button class="action-button is-danger" type="button"
            data-wizard-attr-remove="${index}" ${draft.attributes.length <= 1 ? "disabled" : ""}>移除</button>
        </div>`,
      )
      .join("");
    return `
      <label class="field-label">检定模式</label>
      <select class="wizard-select" data-wizard="resolution_mode">
        <option value="attribute" ${
          draft.resolution_mode !== "dice_only" ? "selected" : ""
        }>属性检定（角色卡带属性，检定按属性结算）</option>
        <option value="dice_only" ${
          draft.resolution_mode === "dice_only" ? "selected" : ""
        }>纯骰子（不依赖角色卡属性）</option>
      </select>
      <label class="field-label">骰制</label>
      <select class="wizard-select" data-wizard="dice_system">
        <option value="d20" ${
          (draft.dice_system || "d20") === "d20" ? "selected" : ""
        }>d20（默认）</option>
        <option value="d6" ${
          draft.dice_system === "d6" ? "selected" : ""
        }>d6</option>
        <option value="d100" ${
          draft.dice_system === "d100" ? "selected" : ""
        }>d100</option>
      </select>
      <div id="wizard-attr-block">
        <div class="field-label-row">
          <label class="field-label">角色卡属性（检定模式）</label>
          <button class="action-button" type="button" id="wizard-attr-add">＋ 添加属性</button>
        </div>
        <div id="wizard-attr-rows">${attrRows}</div>
      </div>
      <p class="field-hint">预设数值堆叠（preset_stack）等高级数值模式请先创建基础世界，再用高级 JSON 编辑。</p>`;
  }
  if (step === 3) {
    return `
      <div class="wizard-grid-2">
        <label class="field-label">默认 DC（高难度档）</label>
        <input class="wizard-input" type="number" min="1" max="40"
          data-wizard="default_difficulty"
          value="${escapeHTML(String(draft.default_difficulty ?? 12))}" />
        <label class="field-label">难度下限</label>
        <input class="wizard-input" type="number" min="1" max="40"
          data-wizard="difficulty_min"
          value="${escapeHTML(String(draft.difficulty_min ?? 5))}" />
        <label class="field-label">难度上限</label>
        <input class="wizard-input" type="number" min="1" max="40"
          data-wizard="difficulty_max"
          value="${escapeHTML(String(draft.difficulty_max ?? 25))}" />
        <label class="field-label">推荐最少玩家</label>
        <input class="wizard-input" type="number" min="1" max="32"
          data-wizard="recommended_min"
          value="${escapeHTML(String(draft.recommended_min ?? 2))}" />
        <label class="field-label">推荐最多玩家</label>
        <input class="wizard-input" type="number" min="1" max="32"
          data-wizard="recommended_max"
          value="${escapeHTML(String(draft.recommended_max ?? 4))}" />
        <label class="field-label">开团最少人数</label>
        <input class="wizard-input" type="number" min="1" max="32"
          data-wizard="minimum_start"
          value="${escapeHTML(String(draft.minimum_start ?? 2))}" />
        <label class="field-label">席位上限</label>
        <input class="wizard-input" type="number" min="1" max="32"
          data-wizard="maximum"
          value="${escapeHTML(String(draft.maximum ?? 4))}" />
      </div>
      <label class="toggle-line">
        <input type="checkbox" data-wizard="strict_choices" ${
          draft.strict_choices !== false ? "checked" : ""
        } />
        <span>严格选项（剧情只接受 A/B/C/D）</span>
      </label>
      <label class="toggle-line">
        <input type="checkbox" data-wizard="allow_player_result_claims" ${
          draft.allow_player_result_claims ? "checked" : ""
        } />
        <span>允许玩家声明结果（默认关闭，更安全）</span>
      </label>`;
  }
  if (step === 4) {
    return `
      <label class="field-label">开场场景</label>
      <textarea class="wizard-textarea" data-wizard="opening_scene" rows="4"
        placeholder="故事从哪里开始？">${escapeHTML(
          draft.opening_scene || "",
        )}</textarea>
      <div class="wizard-grid-2">
        <label class="field-label">地点</label>
        <input class="wizard-input" data-wizard="location" value="${escapeHTML(
          draft.location || "",
        )}" placeholder="如：雾屿东港码头" />
        <label class="field-label">时间</label>
        <input class="wizard-input" data-wizard="time" value="${escapeHTML(
          draft.time || "",
        )}" placeholder="如：暮春清晨" />
      </div>
      <label class="field-label">场景摘要</label>
      <input class="wizard-input" data-wizard="scene_summary" value="${escapeHTML(
        draft.scene_summary || "",
      )}" placeholder="当前局面的一句话概括" />
      <label class="field-label">已发生事实（每行一条）</label>
      <textarea class="wizard-textarea" data-wizard="facts" rows="4"
        placeholder="队伍已抵达码头&#10;雾中灯塔三长两短">${escapeHTML(
          draft.facts || "",
        )}</textarea>`;
  }
  if (step === 5) {
    const world = wizardDraftToWorld();
    const issues = (wizardState.preflight?.issues || [])
      .map((issue) => `<li>${escapeHTML(String(issue.message || ""))}</li>`)
      .join("");
    const summary = JSON.stringify(world, null, 2);
    return `
      <div class="wizard-summary">
        <div class="wizard-summary-row">
          <span>世界</span><strong>${escapeHTML(world.name)}</strong>
        </div>
        <div class="wizard-summary-row">
          <span>标识</span><code>${escapeHTML(world.slug)}</code>
        </div>
        <div class="wizard-summary-row">
          <span>模式</span><span>${escapeHTML(
            world.rules.resolution.mode === "dice_only" ? "纯骰子" : "属性检定",
          )} · ${escapeHTML(world.rules.resolution.dice_system)}</span>
        </div>
        <div class="wizard-summary-row">
          <span>席位</span><span>${escapeHTML(
            String(world.rules.player_limits.recommended_min),
          )}–${escapeHTML(
            String(world.rules.player_limits.recommended_max),
          )}（上限 ${escapeHTML(String(world.rules.player_limits.maximum))}）</span>
        </div>
        <details>
          <summary>世界体检报告（${
            wizardState.preflight?.compatible ? "通过" : "待体检"
          }）</summary>
          <ul class="issue-list">${
            issues || "<li>尚未体检</li>"
          }</ul>
        </details>
        <details>
          <summary>生成的世界包 JSON</summary>
          <pre class="json-preview">${escapeHTML(summary)}</pre>
        </details>
      </div>`;
  }
  return "";
}

function renderWizard() {
  const draft = wizardState.draft;
  $("#wizard-steps").innerHTML = WIZARD_STEPS.map(
    ([label, hint], index) => `
      <div class="wizard-step ${
        index + 1 === wizardState.step
          ? "is-active"
          : index + 1 < wizardState.step
            ? "is-done"
            : ""
      }">
        <span class="wizard-step-no">${index + 1}</span>
        <div>
          <strong>${escapeHTML(label)}</strong>
          <span>${escapeHTML(hint)}</span>
        </div>
      </div>`,
  ).join("");
  $("#wizard-title").textContent = `新建世界 · 第 ${wizardState.step} / 5 步`;
  $("#wizard-body").innerHTML = wizardStepMarkup(wizardState.step);
  $("#wizard-prev").disabled = wizardState.step <= 1;
  const next = $("#wizard-next");
  next.textContent = wizardState.step >= 5 ? "导入世界" : "下一步";
  if (wizardState.step === 5 && !wizardState.preflight) {
    runWizardPreflight();
  }
  saveWizardDraft();
}

async function runWizardPreflight() {
  const world = wizardDraftToWorld();
  try {
    const report = await bridge.apiPost("worlds/preflight", { world });
    wizardState.preflight = report;
    if (wizardState.step === 5) {
      $("#wizard-body").innerHTML = wizardStepMarkup(5);
    }
  } catch (error) {
    wizardState.preflight = {
      compatible: false,
      issues: [{ message: error?.message || String(error) }],
    };
    if (wizardState.step === 5) {
      $("#wizard-body").innerHTML = wizardStepMarkup(5);
    }
  }
}

function openWorldWizard() {
  loadWizardDraft();
  wizardState.step = 1;
  wizardState.preflight = null;
  $("#world-wizard-modal").showModal();
  renderWizard();
}

function bindWizardEvents() {
  $("#world-wizard-button").addEventListener("click", openWorldWizard);
  $("#wizard-prev").addEventListener("click", () => {
    if (wizardState.step > 1) {
      wizardState.step -= 1;
      renderWizard();
    }
  });
  $("#wizard-next").addEventListener("click", async () => {
    if (wizardState.step >= 5) {
      const world = wizardDraftToWorld();
      try {
        await submitWorldImport(world, "auto");
        closeWizard();
      } catch (error) {
        showError(error);
      }
      return;
    }
    wizardState.step += 1;
    renderWizard();
  });
  // 输入即存草稿（委托）
  $("#world-wizard-form").addEventListener("input", (event) => {
    const field = event.target.closest("[data-wizard]");
    if (field) {
      const key = field.dataset.wizard;
      wizardState.draft[key] =
        field.type === "checkbox" ? field.checked : field.value;
      saveWizardDraft();
      return;
    }
    const attrField = event.target.closest("[data-wizard-attr]");
    if (attrField) {
      const index = Number(attrField.dataset.index);
      wizardState.draft.attributes[index] = {
        ...wizardState.draft.attributes[index],
        [attrField.dataset.wizardAttr]: attrField.value,
      };
      saveWizardDraft();
    }
  });
  $("#world-wizard-form").addEventListener("click", (event) => {
    const add = event.target.closest("#wizard-attr-add");
    if (add) {
      wizardState.draft.attributes = [
        ...wizardState.draft.attributes,
        { key: "", label: "" },
      ];
      renderWizard();
      return;
    }
    const remove = event.target.closest("[data-wizard-attr-remove]");
    if (remove) {
      const index = Number(remove.dataset.wizardAttrRemove);
      wizardState.draft.attributes = wizardState.draft.attributes.filter(
        (_, itemIndex) => itemIndex !== index,
      );
      if (!wizardState.draft.attributes.length) {
        wizardState.draft.attributes = [{ key: "", label: "" }];
      }
      renderWizard();
      return;
    }
    const step = event.target.closest(".wizard-step");
    if (step && wizardState.draft) {
      const target = [...step.parentElement.children].indexOf(step);
      wizardState.step = target + 1;
      renderWizard();
    }
  });
}

function closeWizard() {
  clearWizardDraft();
  $("#world-wizard-modal").close();
}

// A14（审计 F1）：对 bridge 的 GET/POST 做网络级重试（5xx / 连接 / 超时），
// 避免瞬时网络抖动直接 toast 失败；业务错误（4xx）不重试。
function wrapBridgeWithRetry() {
  const retryable = (error) => {
    const text = String(error?.message || "");
    return /5\d\d|network|ECONN|socket|timed? ?out|Failed to fetch|ERR_|ETIMEDOUT/i.test(
      text,
    );
  };
  const withRetry = async (fn, args, attempts = 3) => {
    let last;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        return await fn(...args);
      } catch (error) {
        last = error;
        if (!retryable(error) || attempt === attempts - 1) throw error;
        await new Promise((resolve) =>
          setTimeout(resolve, 400 * (attempt + 1)),
        );
      }
    }
    throw last;
  };
  const get = bridge.apiGet?.bind(bridge);
  const post = bridge.apiPost?.bind(bridge);
  if (get) bridge.apiGet = (...args) => withRetry(get, args);
  if (post) bridge.apiPost = (...args) => withRetry(post, args);
}

async function boot() {
  try {
    app.context = await bridge.ready();
    applyBridgeContext(app.context);
    wrapBridgeWithRetry();
    bindV0120();
    bindWizardEvents();
    if (typeof bridge.onContext === "function") {
      app.contextOff = bridge.onContext((context) => {
        app.context = context;
        applyBridgeContext(context);
      });
    }
    await loadCore();
    await startSSE();
  } catch (error) {
    setConnection("error", "连接失败");
    showError(error);
  }
}

boot();
