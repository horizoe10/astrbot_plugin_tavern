from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "console"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.assets: list[str] = []
        self.elements_by_id: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if values.get("id"):
            element_id = str(values["id"])
            self.ids.append(element_id)
            self.elements_by_id[element_id] = (tag, values)
        for name in ("src", "href"):
            value = values.get(name)
            if value:
                self.assets.append(value)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)


class StaticUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (PAGE / "index.html").read_text(encoding="utf-8")
        cls.script = (PAGE / "app.js").read_text(encoding="utf-8")
        cls.style = (PAGE / "style.css").read_text(encoding="utf-8")
        cls.parser = _PageParser()
        cls.parser.feed(cls.html)
        cls.bridge_js = (PAGE / "core" / "bridge.js").read_text(encoding="utf-8")

    def test_static_ids_are_unique(self) -> None:
        duplicates = {
            item
            for item in self.parser.ids
            if self.parser.ids.count(item) > 1
        }
        self.assertEqual(duplicates, set())

    def test_command_trigger_help_names_every_supported_separator(self) -> None:
        start = self.html.index('<label for="setting-command-triggers">')
        end = self.html.index("</div>", start)
        field = self.html[start:end]

        self.assertIn(
            "多个触发词可使用换行、英文逗号（,）或中文逗号（，）分隔。",
            field,
        )
        self.assertIn("完全取代 /酒馆", field)
        self.assertIn("最多 8 项、每项最多 16", field)
        self.assertIn("不得与剧情触发前缀相同", field)

    def test_extensions_panel_has_resilient_loading_and_retry_states(self) -> None:
        self.assertIn("Promise.allSettled", self.script)
        self.assertIn("正在读取扩展菜单", self.script)
        self.assertIn("扩展菜单读取失败", self.script)
        self.assertIn("暂无已注册扩展", self.script)
        self.assertIn("about-extensions-retry", self.script)
        self.assertIn("prefers-reduced-motion:reduce", self.style)

    def test_all_local_assets_exist(self) -> None:
        for asset in self.parser.assets:
            if asset.startswith(("http://", "https://", "/", "#")):
                continue
            resolved = (PAGE / asset.split("?", 1)[0]).resolve()
            self.assertTrue(
                resolved.is_file(),
                f"missing page asset: {asset}",
            )

    def test_javascript_id_references_have_a_template_element(self) -> None:
        declared = set(self.parser.ids)
        declared.update(
            re.findall(r"""\bid=["']([A-Za-z0-9_-]+)["']""", self.script)
        )
        referenced = set(
            re.findall(r"""["']#([A-Za-z][A-Za-z0-9_-]*)["']""", self.script)
        )
        missing = referenced - declared
        self.assertEqual(missing, set())

    def test_css_variables_are_defined(self) -> None:
        defined = set(re.findall(r"--([a-z0-9-]+)\s*:", self.style))
        used = set(re.findall(r"var\(--([a-z0-9-]+)", self.style))
        self.assertEqual(used - defined, set())

    def test_page_is_self_contained_and_uses_bridge(self) -> None:
        combined = self.html + self.script + self.style + self.bridge_js
        allowed_repository = "https://github.com/horizoe10/astrbot_plugin_tavern"
        self.assertNotIn("https://", combined.replace(allowed_repository, ""))
        self.assertNotIn("http://", combined)
        self.assertIn("window.AstrBotPluginPage", combined)
        # 世界向导草稿持久化（WIZARD_DRAFT_KEY）允许使用 localStorage，
        # 且实现已对不可用环境做 try/catch 静默降级；禁止使用 cookie。
        self.assertNotIn("document.cookie", self.script)
        self.assertIn('type="module"', self.html)

    def test_editor_cancel_controls_cannot_submit(self) -> None:
        for element_id in ("editor-modal-close", "editor-cancel-button"):
            tag, attrs = self.parser.elements_by_id[element_id]
            self.assertEqual(tag, "button")
            self.assertEqual(attrs.get("type"), "button")
        save_tag, save_attrs = self.parser.elements_by_id[
            "editor-save-button"
        ]
        self.assertEqual(save_tag, "button")
        self.assertEqual(save_attrs.get("type"), "submit")
        self.assertIn(
            'event.submitter?.id !== "editor-save-button"',
            self.script,
        )

    def test_memory_uses_list_rows_and_model_controls_exist(self) -> None:
        self.assertIn(
            'class="memory-list" id="memory-grid"',
            self.html,
        )
        self.assertIn('class="memory-row"', self.script)
        self.assertNotIn('class="memory-card"', self.script)
        for element_id in (
            "setting-provider",
            "fallback-provider-list",
            "setting-image-provider",
            "setting-image-prompt",
        ):
            self.assertIn(element_id, self.parser.elements_by_id)

    def test_v051_session_search_and_template_import_are_visible(self) -> None:
        for element_id in (
            "session-search",
            "session-search-scope",
            "session-search-clear",
            "session-result-count",
            "session-page-prev",
            "session-page-next",
        ):
            self.assertIn(element_id, self.parser.elements_by_id)
        for marker in (
            'data-group-action="remark"',
            'data-world-action="card-template"',
            'id="character-template-import"',
            'id="character-template-export"',
            'id="character-template-preview-button"',
            "validateCharacterCardTemplate",
        ):
            self.assertIn(marker, self.script)

    def test_session_roster_renders_complete_character_cards(self) -> None:
        for marker in (
            "renderRosterCharacterCard",
            "renderCharacterCardFields",
            "renderCharacterCardStats",
            "renderCharacterRuntimeState",
            "character_card_template",
            'class="roster-character-card"',
            'class="character-card-fields"',
            'class="character-card-stats"',
            'class="character-card-records"',
        ):
            self.assertIn(marker, self.script)
        for selector in (
            ".roster-character-card",
            ".character-card-fields",
            ".character-card-stats",
            ".character-card-records",
        ):
            self.assertIn(selector, self.style)

    def test_v053_runtime_controls_are_visible(self) -> None:
        for marker in (
            'data-session-detail-action="force-ready"',
            'data-timer-policy="all"',
            'data-group-action="token-quota"',
            'id="group-quota-enabled"',
            'id="quota-session-enabled"',
            'data-session-detail-action="save-session-token-quota"',
            'data-session-detail-action="delete-independent-save"',
            'data-session-detail-action="delete-session"',
            '"groups/token-quota"',
            '"sessions/token-quota"',
            '"sessions/timer-policy"',
            '"archives/delete"',
        ):
            self.assertIn(marker, self.script)



    def test_local_storage_usage_is_sandbox_safe(self) -> None:
        """AstrBot 插件页运行在沙箱 iframe，window.localStorage 访问可能抛
        SecurityError；所有 getItem/setItem/removeItem 必须经 safeLocalStorage()。
        """
        self.assertIn("function safeLocalStorage", self.script)
        direct = re.findall(
            r"(?<![\w.?])localStorage\.(getItem|setItem|removeItem)",
            self.script,
        )
        self.assertEqual(direct, [])

    def test_manifest_schema_and_i18n_are_consistent(self) -> None:
        metadata = yaml.safe_load(
            (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["name"], "astrbot_plugin_tavern")
        self.assertEqual(metadata["astrbot_version"], ">=4.26,<5")
        self.assertIn("aiocqhttp", metadata["support_platforms"])

        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            {"security", "model", "runtime", "advanced"}.issubset(schema)
        )
        self.assertEqual(
            schema["runtime"]["items"]["trigger_prefix"]["default"],
            "jg",
        )
        self.assertEqual(metadata["version"], "0.12.0")
        self.assertEqual(
            metadata["repo"],
            "https://github.com/horizoe10/astrbot_plugin_tavern",
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("QQ 群 `1094220887`", readme)
        self.assertIn("https://github.com/horizoe10/astrbot_plugin_tavern", self.script)
        self.assertIn("<tr><td>仓库</td>", self.script)
        self.assertEqual(
            schema["model"]["items"]["provider_id"]["_special"],
            "select_provider",
        )
        self.assertEqual(
            schema["model"]["items"]["image_caption_provider_id"][
                "_special"
            ],
            "select_provider",
        )
        for index in range(1, 5):
            self.assertEqual(
                schema["model"]["items"][
                    f"fallback_provider_{index}_id"
                ]["_special"],
                "select_provider",
            )
        for locale in ("zh-CN", "en-US"):
            messages = json.loads(
                (
                    ROOT
                    / ".astrbot-plugin"
                    / "i18n"
                    / f"{locale}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("console", messages["pages"])

    def test_card_review_notification_schema(self) -> None:
        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        notification_mode = schema["runtime"]["items"][
            "card_review_notification_mode"
        ]
        self.assertEqual(notification_mode["default"], "both")
        self.assertEqual(
            notification_mode["options"],
            ["both", "group", "private"],
        )
        self.assertEqual(
            notification_mode["labels"],
            [
                "私聊和群聊同时通知",
                "仅群聊 @ 成员",
                "仅私聊通知",
            ],
        )

    def test_card_review_notification_selector(self) -> None:
        self.assertIn(
            'id="setting-card-review-notification-mode"',
            self.html,
        )
        for mode in ("both", "group", "private"):
            self.assertIn(f'<option value="{mode}">', self.html)
        self.assertIn(
            's.runtime.card_review_notification_mode || "both"',
            self.script,
        )

    def test_command_trigger_schema(self) -> None:
        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        item = schema["runtime"]["items"]["command_triggers"]
        self.assertEqual(item["type"], "list")
        self.assertEqual(item["default"], ["酒馆"])
        for marker in ("完全取代", "8", "16", "剧情触发"):
            self.assertIn(marker, item["hint"])

    def test_command_trigger_editor(self) -> None:
        self.assertIn('id="setting-command-triggers"', self.html)
        self.assertIn(
            '(s.runtime.command_triggers || ["酒馆"]).join("\\n")',
            self.script,
        )
        self.assertIn("split(/[\\n,，]+/)", self.script)
        self.assertIn(
            'card_review_notification_mode: $("#setting-card-review-notification-mode").value',
            self.script,
        )


if __name__ == "__main__":
    unittest.main()

