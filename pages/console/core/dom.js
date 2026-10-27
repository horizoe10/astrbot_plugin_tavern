export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function prettyJSON(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

export function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

export function statusLabel(status) {
  return ({
    running: "运行中",
    preparing: "准备中",
    paused: "已暂停",
    finished: "已完结",
    maintenance: "维护中",
    closed: "已关闭",
  })[status] || status;
}

export function statusBadge(status) {
  return `<span class="status-badge status-${escapeHTML(status)}">${escapeHTML(
    statusLabel(status),
  )}</span>`;
}

export function snapshotKindLabel(kind) {
  return ({
    manual: "手动存档",
    auto: "自动检查点",
    safety: "操作前保护",
    undo: "单回合回滚点",
  })[kind] || kind;
}
