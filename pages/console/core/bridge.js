export const bridge = window.AstrBotPluginPage;

if (!bridge) {
  throw new Error("AstrBot 管理台桥接对象不可用");
}
