const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require(
  path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES, "playwright"),
);

const target =
  process.argv[2] || "http://127.0.0.1:8765/pages/console/index.html";
const screenshotDir = process.argv[3] || "/tmp";

function installMockBridge() {
  window.__mockPosts = [];
  window.__mockGets = [];
  const world = {
    id: "world_demo",
    slug: "border-tavern",
    name: "边境无名酒馆",
    description: "一座位于诸界夹缝中的中立酒馆。",
    system_prompt: "这是一个低魔、克制、因果连续的奇幻世界。",
    opening_scene: "夜雨沿着黑杉木檐滴落。",
    rules: {
      resolution: "d20",
      default_difficulty: 12,
    },
    initial_state: {
      location: "边境无名酒馆·大厅",
      time: "雨夜",
      scene_summary: "壁炉低燃，铜铃无风轻响。",
      facts: ["酒馆保持中立"],
      inventory: {},
      relationships: {},
    },
    archived: false,
    revision: 4,
    character_count: 3,
    updated_at: "2026-07-27T12:00:00+00:00",
  };
  const session = {
    id: "session_demo",
    platform_id: "aiocqhttp",
    group_id: "78210431",
    instance_slug: "rain-night",
    instance_name: "雨夜一号副本",
    selected: true,
    world_id: world.id,
    world_name: world.name,
    world_slug: world.slug,
    state: "running",
    turn_no: 18,
    revision: 22,
    player_count: 4,
    world_state: {
      location: "边境无名酒馆·大厅",
      time: "雨夜·第三刻",
      scene_summary: "陌生信使把一封湿透的信留在柜台。",
      facts: ["酒馆保持中立", "信封带有旧王庭火漆"],
      inventory: {},
      relationships: {},
    },
    updated_at: "2026-07-27T12:08:00+00:00",
    created_at: "2026-07-20T12:08:00+00:00",
    group_remark: "周六固定团",
    group_revision: 2,
    playthrough_no: 1,
    storage_sync_status: "ready",
  };
  const settings = {
    security: {
      admin_ids: ["100001"],
      allowed_group_ids: ["78210431"],
      require_group_whitelist: true,
      unauthorized_command_behavior: "silent",
      public_status: true,
    },
    model: {
      provider_id: "story-primary",
      fallback_provider_ids: ["story-backup-a", "story-backup-b"],
      image_caption_provider_id: "vision-caption",
      image_caption_prompt: "只描述图片中可见且与行动有关的信息。",
      max_images_per_turn: 3,
      temperature: 0.7,
      max_tokens: 1800,
      request_timeout_seconds: 120,
      json_repair_attempts: 1,
    },
    runtime: {
      default_world_slug: "border-tavern",
      trigger_prefix: "jg",
      command_triggers: ["团", "跑团"],
      two_phase_checks: true,
      max_input_chars: 2000,
      max_output_chars: 5000,
      recent_turns: 12,
      memory_limit: 10,
      user_cooldown_seconds: 1.5,
      auto_snapshot_interval: 5,
      card_review_notification_mode: "both",
      ooc_prefixes: ["【OOC】", "[OOC]", "OOC:"],
    },
    advanced: {
      audit_retention_days: 90,
      store_model_payloads: false,
      debug: false,
    },
  };
  const providers = [
    { id: "story-primary", name: "主叙事", model: "narrative-pro" },
    { id: "story-backup-a", name: "备用甲", model: "narrative-fast" },
    { id: "story-backup-b", name: "备用乙", model: "narrative-safe" },
    { id: "vision-caption", name: "图片转述", model: "vision-pro" },
  ];
  const memories = [
    {
      id: "memory_1",
      session_id: session.id,
      scope: "world",
      scope_id: "",
      kind: "fact",
      content: "旧王庭火漆只会在午夜后的蓝焰中显出暗纹。",
      importance: 5,
      salience: 1,
      tags: ["旧王庭", "火漆"],
      updated_at: "2026-07-27T12:08:00+00:00",
    },
  ];
  const characters = [
    {
      id: "character_keeper",
      world_id: world.id,
      slug: "keeper",
      name: "无名掌柜",
      role: "npc",
      profile: {
        identity: "酒馆管理者",
        knowledge_boundary: "只知道在酒馆内成交的约定",
      },
      prompt: "克制、谨慎，不主动泄露来客秘密。",
      enabled: true,
      sort_order: 0,
      revision: 1,
    },
  ];
  const turn = {
    round_no: 5,
    current_user_id: "200001",
    current_name: "塞拉",
    order: [
      {
        position: 1,
        player_id: "player_1",
        user_id: "200001",
        display_name: "旅客",
        character_name: "塞拉",
        name: "塞拉",
      },
    ],
  };
  const characterCardTemplate = {
    version: 1,
    auto_approve: false,
    edit_requires_review: true,
    fields: [
      ["name", "角色姓名", true, false],
      ["code", "副本代号", true, false],
      ["appearance", "外貌特征", true, false],
      ["background", "角色背景", true, false],
      ["goal", "当前目标", true, false],
      ["secret", "私人秘密", false, true],
    ].map(([key, label, required, privateField]) => ({
      key,
      label,
      required,
      private: privateField,
      max_chars: 500,
      type: "text",
    })),
    stats: {
      budget: 10,
      attributes: [
        ["body", "体魄", 3],
        ["agility", "敏捷", 3],
        ["will", "意志", 2],
        ["knowledge", "学识", 2],
      ].map(([key, label, defaultValue]) => ({
        key,
        label,
        minimum: 0,
        maximum: 5,
        default: defaultValue,
      })),
      modifier_table: { 0: -3, 1: -2, 2: -1, 3: 0, 4: 1, 5: 2 },
    },
  };
  const roster = [
    {
      id: "participant_1",
      group_user_id: "200001",
      display_name: "旅客",
      character_name: "塞拉",
      character_code: "SL",
      aliases: ["灰羽"],
      card_status: "pending_review",
      ready: false,
      participation_status: "reserved",
      joined_round: 1,
      consecutive_timeouts: 0,
      card_profile: {
        name: "塞拉",
        code: "SL",
        appearance: "银灰色短发，披着沾有雨水的深色斗篷。",
        background: "来自北方边境的信使，熟悉废弃驿道。",
        goal: "确认湿透信封真正的收件人。",
        secret: "她认得火漆上的旧王庭暗纹。",
        stat_body: 3,
        stat_agility: 3,
        stat_will: 2,
        stat_knowledge: 2,
      },
      card_stats: {
        raw: { body: 3, agility: 3, will: 2, knowledge: 2 },
        labels: {
          body: "体魄",
          agility: "敏捷",
          will: "意志",
          knowledge: "学识",
        },
        modifiers: { body: 0, agility: 0, will: -1, knowledge: -1 },
        budget: 10,
      },
      runtime_state: {
        inspiration: 1,
        inspiration_max: 3,
        statuses: ["淋湿"],
        equipment: { weapon: "短剑", letter: "湿透的信封" },
        known_clues: ["旧王庭火漆"],
        current_location: "酒馆大厅",
      },
      runtime_revision: 2,
      card_version_no: 1,
      card_template_version: 1,
      card_version_status: "pending_review",
      card_review_note: "",
      updated_at: "2026-07-27T12:08:00+00:00",
    },
  ];
  const apiGet = async (endpoint, params = {}) => {
    window.__mockGets.push({ endpoint, params });
    if (endpoint === "overview") {
      return {
        counts: {
          running: 1,
          sessions: 2,
          worlds: 3,
          memories: 46,
          snapshots: 11,
        },
        database_size: 188416,
        database_ok: true,
        schema_version: 12,
        plugin_version: "0.12.0",
        security: {
          admin_count: 1,
          allowed_group_count: 1,
          whitelist_required: true,
          ready: true,
        },
        sessions: [session],
      };
    }
    if (endpoint === "worlds") return { items: [world] };
    if (endpoint === "sessions") {
      return {
        items: [session],
        options: [session],
        groups: [
          {
            platform_id: session.platform_id,
            group_id: session.group_id,
            remark: session.group_remark,
            revision: session.group_revision,
            story_count: 1,
            running_count: 1,
          },
        ],
        total: 1,
        page: 1,
        pages: 1,
      };
    }
    if (endpoint === "settings") return { settings };
    if (endpoint === "providers") return { items: providers };
    if (endpoint === "characters") return { items: characters };
    if (endpoint === "memories") return { items: memories };
    if (endpoint === "audit") return { items: [] };
    if (endpoint === "groups/token-usage") {
      return {
        usage: {
          platform_id: session.platform_id,
          group_id: session.group_id,
          session_id: session.id,
          group: { hour: 1200, day: 8600, all: 45200 },
          quota: {
            scope_type: "group",
            window_seconds: 86400,
            token_limit: 500000,
            enabled: true,
            used: 8600,
            remaining: 491400,
          },
        },
      };
    }
    if (endpoint === "sessions/detail") {
      return {
        session,
        turn,
        players: [
          {
            id: "player_1",
            user_id: "200001",
            display_name: "旅客",
            character_name: "塞拉",
            profile: {},
            enabled: true,
          },
        ],
        roster,
        instance_config: {
          session_id: session.id,
          world_revision: 4,
          world_snapshot: world,
          character_card_template: characterCardTemplate,
          time_rules: {},
          phase_meta: {},
        },
        preflight: {
          ok: false,
          blockers: ["塞拉的角色卡尚未通过审核"],
        },
        snapshots: [
          {
            id: "save_1",
            name: "进入旧塔之前",
            kind: "manual",
            turn_no: 17,
            created_by: "100001",
            created_at: "2026-07-27T12:05:00+00:00",
          },
        ],
        events: [
          {
            role: "narrator",
            actor_name: "酒馆叙事者",
            turn_no: 18,
            content: "湿透的信封在烛光下显出暗红火漆。",
            created_at: "2026-07-27T12:08:00+00:00",
          },
        ],
        storage: {
          relative_path:
            "groups/aiocqhttp_g_demo/stories/rain-night_20260720120800_i-demo",
          database_exists: true,
          manifest_exists: true,
          sync_status: "ready",
          save_files: [
            {
              filename: "save_rain-night_20260727120500.zip",
              size: 20480,
              created_at: "2026-07-27T12:05:00+00:00",
            },
          ],
          backup_files: [],
        },
      };
    }
    throw new Error(`Unhandled GET ${endpoint}`);
  };
  window.AstrBotPluginPage = {
    ready: async () => ({
      pluginName: "astrbot_plugin_tavern",
      pageName: "console",
      locale: "zh-CN",
      isDark: true,
    }),
    t: (_key, fallback) => fallback,
    onContext: () => () => {},
    apiGet,
    apiPost: async (endpoint, body) => {
      window.__mockPosts.push({ endpoint, body });
      return { item: body, settings };
    },
    subscribeSSE: async (_endpoint, handlers) => {
      handlers.onOpen?.();
      return "mock-sse";
    },
    unsubscribeSSE: async () => {},
    download: async () => ({ filename: "tavern-backup.json" }),
    upload: async () => ({ imported: {} }),
  };
}

async function installTestFont(page) {
  if (!process.env.TAVERN_FONT_CSS_URL) return;
  const fontCssUrl = JSON.stringify(process.env.TAVERN_FONT_CSS_URL);
  await page.addStyleTag({
    content: `
      @import url(${fontCssUrl});
      html, body, button, input, select, textarea {
        font-family: "Noto Sans SC Variable", sans-serif !important;
      }
    `,
  });
  await page.evaluate(() => document.fonts.ready);
}

(async () => {
  const executablePath =
    process.env.TAVERN_CHROMIUM_PATH || chromium.executablePath();
  if (!fs.existsSync(executablePath)) {
    console.log(
      `UI smoke test skipped: Playwright Chromium is not installed (${executablePath})`,
    );
    return;
  }
  const browser = await chromium.launch({
    headless: true,
    executablePath,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.addInitScript(installMockBridge);
  await page.goto(target, { waitUntil: "networkidle" });
  await installTestFont(page);
  await page.waitForSelector("#metrics .metric-card");
  assert.equal(await page.locator("#metrics .metric-card").count(), 4);
  assert.match(
    await page.locator("#live-text").textContent(),
    /运行正常|实时连接/,
  );
  await page.screenshot({
    path: path.join(screenshotDir, "tavern-console-desktop.png"),
    fullPage: true,
  });

  await page.locator('[data-view="worlds"]').click();
  await page.locator('[data-world-action="card-template"]').click();
  assert.match(
    await page.locator("#editor-modal-title").textContent(),
    /角色卡模板/,
  );
  assert.equal(
    await page.locator("#character-template-import").textContent(),
    "导入 JSON",
  );
  await page
    .locator("#editor-cancel-button")
    .evaluate((element) => element.click());
  assert.equal(await page.locator("#editor-modal").getAttribute("open"), null);

  await page.locator('[data-world-action="characters"]').click();
  assert.equal(await page.locator("#editor-modal").getAttribute("open"), "");
  assert.match(
    await page.locator("#editor-modal-title").textContent(),
    /角色管理/,
  );
  await page
    .locator("#editor-cancel-button")
    .evaluate((element) => element.click());
  assert.equal(await page.locator("#editor-modal").getAttribute("open"), null);
  assert.equal(await page.evaluate(() => window.__mockPosts.length), 0);

  await page.locator('[data-view="sessions"]').click();
  await page.locator("#session-search").fill("周六固定团");
  await page.waitForFunction(() =>
    window.__mockGets.some(
      (item) =>
        item.endpoint === "sessions" &&
        item.params?.q === "周六固定团",
    ),
  );
  assert.match(
    await page.locator("#session-result-count").textContent(),
    /检索到 1 个故事副本/,
  );
  await page.locator('[data-group-action="remark"]').click();
  assert.match(
    await page.locator("#editor-modal-title").textContent(),
    /编辑群备注/,
  );
  await page
    .locator("#editor-cancel-button")
    .evaluate((element) => element.click());
  await page.locator('[data-group-action="token-quota"]').click();
  assert.match(
    await page.locator("#editor-modal-title").textContent(),
    /Token 限额/,
  );
  assert.equal(
    await page.locator("#group-quota-enabled").isChecked(),
    true,
  );
  await page
    .locator("#editor-cancel-button")
    .evaluate((element) => element.click());
  await page.locator("#session-search-clear").click();
  await page.locator('[data-session-action="detail"]').click();
  await page.locator('[data-session-tab="roster"]').click();
  assert.equal(
    await page.locator(".roster-character-card").count(),
    1,
  );
  assert.match(
    await page.locator(".roster-character-card").textContent(),
    /完整角色资料/,
  );
  assert.match(
    await page.locator(".roster-character-card").textContent(),
    /她认得火漆上的旧王庭暗纹/,
  );
  assert.equal(
    await page.locator(".character-card-stat").count(),
    4,
  );
  await page.screenshot({
    path: path.join(screenshotDir, "tavern-console-character-cards.png"),
    fullPage: true,
  });
  await page.locator("#session-modal-close").click();

  await page.locator("#new-session-button").click();
  assert.match(
    await page.locator("#editor-modal-title").textContent(),
    /建立群会话/,
  );
  await page
    .locator("#editor-cancel-button")
    .evaluate((element) => element.click());
  assert.equal(await page.locator("#editor-modal").getAttribute("open"), null);
  assert.equal(await page.evaluate(() => window.__mockPosts.length), 0);

  await page.locator("#new-session-button").click();
  await page
    .locator("#editor-save-button")
    .evaluate((element) => element.click());
  assert.equal(await page.locator("#editor-modal").getAttribute("open"), "");
  assert.equal(await page.evaluate(() => window.__mockPosts.length), 0);
  assert.equal(
    await page.locator("#editor-save-button").textContent(),
    "建立会话",
  );

  await page.locator("#session-platform").evaluate((element) => {
    element.value = "  qq-instance  ";
  });
  await page.locator("#session-group").evaluate((element) => {
    element.value = "  78210432  ";
  });
  await page
    .locator("#editor-save-button")
    .evaluate((element) => element.click());
  await page.waitForFunction(
    () => !document.querySelector("#editor-modal").open,
  );
  const posts = await page.evaluate(() => window.__mockPosts);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].endpoint, "sessions/action");
  assert.equal(posts[0].body.platform_id, "qq-instance");
  assert.equal(posts[0].body.group_id, "78210432");
  assert.equal(posts[0].body.instance_name, "边境无名酒馆");
  assert.equal(posts[0].body.instance_slug, "border-tavern");

  await page.locator('[data-view="settings"]').click();
  assert.equal(await page.locator("#setting-provider").inputValue(), "story-primary");
  assert.equal(
    await page.locator("#setting-image-provider").inputValue(),
    "vision-caption",
  );
  assert.equal(
    await page.locator("#fallback-provider-list .provider-fallback-row").count(),
    2,
  );
  assert.equal(
    await page.locator("#setting-card-review-notification-mode").inputValue(),
    "both",
  );
  assert.equal(
    await page.locator("#setting-command-triggers").inputValue(),
    "团\n跑团",
  );
  await page.locator("#setting-command-triggers").fill("新团，探险团\nTAVERN");
  await page
    .locator("#setting-card-review-notification-mode")
    .selectOption("group");
  await page
    .locator('[data-fallback-action="up"][data-index="1"]')
    .click();
  assert.equal(
    await page
      .locator("[data-fallback-provider]")
      .first()
      .inputValue(),
    "story-backup-b",
  );
  await page.locator('#settings-form button[type="submit"]').click();
  await page.waitForFunction(
    () =>
      window.__mockPosts.some(
        (item) => item.endpoint === "settings/save",
      ),
  );
  const settingPost = (
    await page.evaluate(() => window.__mockPosts)
  ).find((item) => item.endpoint === "settings/save");
  assert.deepEqual(settingPost.body.model.fallback_provider_ids, [
    "story-backup-b",
    "story-backup-a",
  ]);
  assert.equal(
    settingPost.body.runtime.card_review_notification_mode,
    "group",
  );
  assert.deepEqual(settingPost.body.runtime.command_triggers, [
    "新团",
    "探险团",
    "TAVERN",
  ]);
  await page.screenshot({
    path: path.join(screenshotDir, "tavern-console-settings.png"),
    fullPage: true,
  });

  await page.locator('[data-view="memories"]').click();
  await page.waitForSelector("#memory-grid .memory-row");
  assert.equal(await page.locator("#memory-grid .memory-row").count(), 1);
  assert.match(
    await page.locator("#memory-grid .memory-content").textContent(),
    /旧王庭火漆/,
  );
  assert.equal(await page.locator("#memory-grid .world-card").count(), 0);
  await page.waitForTimeout(300);
  await page.screenshot({
    path: path.join(screenshotDir, "tavern-console-memories.png"),
    fullPage: true,
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle" });
  await installTestFont(page);
  await page.waitForSelector("#metrics .metric-card");
  await page.locator("#menu-toggle").click();
  assert.match(await page.locator("#sidebar").getAttribute("class"), /is-open/);
  await page.waitForTimeout(250);
  await page.screenshot({
    path: path.join(screenshotDir, "tavern-console-mobile.png"),
    fullPage: true,
  });

  assert.deepEqual(errors, []);
  await browser.close();
  console.log("UI smoke test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
