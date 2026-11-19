import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import formatting
import ghbuild
import memory_store
import providers
import tools
import webhook_relay


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path = memory_store.DB_PATH
        memory_store.DB_PATH = str(Path(self.temp_dir.name) / "memory.sqlite3")
        await memory_store.init_store()

    async def asyncTearDown(self):
        memory_store.DB_PATH = self.old_path
        self.temp_dir.cleanup()

    async def test_save_list_delete_and_user_isolation(self):
        memory_id, added = await memory_store.add_fact(101, "  Prefiere español  ")
        self.assertTrue(added)
        self.assertEqual(await memory_store.list_facts(101), [(memory_id, "Prefiere español")])
        self.assertEqual(await memory_store.list_facts(202), [])
        self.assertEqual(await memory_store.delete_fact(101, memory_id), 1)
        self.assertEqual(await memory_store.list_facts(101), [])

    async def test_duplicate_and_maximum_length(self):
        first_id, added = await memory_store.add_fact(101, "dato")
        same_id, duplicate_added = await memory_store.add_fact(101, "dato")
        self.assertTrue(added)
        self.assertFalse(duplicate_added)
        self.assertEqual(first_id, same_id)
        with self.assertRaises(ValueError):
            await memory_store.add_fact(101, "x" * (memory_store.MAX_FACT_LENGTH + 1))


class FakeDiscordResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class FakeDiscordSession:
    def __init__(self, status=200):
        self.status = status
        self.url = None
        self.kwargs = None

    def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return FakeDiscordResponse(self.status)


class FakeRequest:
    def __init__(self, payload, origin="https://example.github.io"):
        self.headers = {"Origin": origin}
        self.content_length = len(json.dumps(payload).encode())
        self.content_type = "application/json"
        self.remote = "198.51.100.25"
        self.payload = payload

    async def json(self):
        return self.payload


class WebhookRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_url = webhook_relay.DISCORD_WEBHOOK_URL
        self.old_origins = webhook_relay.WEBHOOK_ALLOWED_ORIGINS
        webhook_relay.DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/123/secret-token"
        webhook_relay.WEBHOOK_ALLOWED_ORIGINS = {"https://example.github.io"}
        webhook_relay._requests_global.clear()
        webhook_relay._requests_by_ip.clear()

    async def asyncTearDown(self):
        webhook_relay.DISCORD_WEBHOOK_URL = self.old_url
        webhook_relay.WEBHOOK_ALLOWED_ORIGINS = self.old_origins
        webhook_relay._requests_global.clear()
        webhook_relay._requests_by_ip.clear()

    async def test_allowed_origin_sends_private_webhook(self):
        session = FakeDiscordSession(status=200)
        response = await webhook_relay.handle_post(
            FakeRequest({"name": "Ana", "message": "Hola"}), session
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["ok"], True)
        self.assertIn("wait=true", session.url)
        self.assertEqual(
            session.kwargs["json"]["allowed_mentions"],
            {"parse": []},
        )
        self.assertEqual(
            session.kwargs["json"]["content"],
            "Nombre: Ana\nMensaje: Hola",
        )

    async def test_rejects_unapproved_origin_without_sending(self):
        session = FakeDiscordSession()
        response = await webhook_relay.handle_post(
            FakeRequest({"content": "hola"}, origin="https://attacker.invalid"),
            session,
        )
        self.assertEqual(response.status, 403)
        self.assertIsNone(session.url)

    async def test_discord_webhook_url_must_be_https(self):
        self.assertTrue(webhook_relay._valid_webhook_url(
            "https://discord.com/api/webhooks/123/token"
        ))
        self.assertFalse(webhook_relay._valid_webhook_url(
            "http://discord.com/api/webhooks/123/token"
        ))


class RoutingTests(unittest.TestCase):
    def test_web_upload_routes_to_pages(self):
        self.assertEqual(bot.detect_build_kind("sube la página web a GitHub"), "pages")
        self.assertEqual(bot.detect_build_kind("sube todo el proyecto a GitHub"), "push")
        self.assertEqual(bot.detect_build_kind("publica mi sitio web"), "pages")

    def test_browser_requests_are_work_mode(self):
        self.assertEqual(bot.detect_mode("navega por internet y busca documentación", False), "work")

    def test_openrouter_models_are_free_and_catalogued(self):
        self.assertTrue(providers.OPENROUTER_MODELS)
        self.assertTrue(all(model.endswith(":free") for model in providers.OPENROUTER_MODELS))
        self.assertTrue(all(providers.ALLOWED_RE.search(model) for model in providers.OPENROUTER_MODELS))
        self.assertEqual(providers.PROVIDER_ORDER, ["horde", "openrouter"])

    def test_generated_html_is_normalized_to_index(self):
        files = ghbuild.norm_files([
            ("script_1.txt", "<!doctype html><h1>Mi web</h1>", "script_1.txt")
        ])
        self.assertEqual(ghbuild.pick_index([(path, content) for _, content, path in files]),
                         ("script_1.html", "<!doctype html><h1>Mi web</h1>"))

    def test_website_attachment_keeps_index_html_filename(self):
        source = "### archivo: index.html\n```html\n<!doctype html>\n<html>\n<body>ok</body>\n</html>\n```"
        _, files = formatting.split_output(source)
        self.assertEqual(files[0][0], "index.html")
        self.assertEqual(files[0][2], "index.html")


class FakeToolResponse:
    status = 204
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def text(self):
        return ""


class FakeToolSession:
    def __init__(self):
        self.url = None
        self.kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def request(self, method, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return FakeToolResponse()


class DirectWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_204_is_reported_as_success(self):
        session = FakeToolSession()
        with patch.dict("os.environ", {"OWNER_IDS": "101"}), \
             patch.object(tools, "_is_safe", return_value=True), \
             patch.object(tools.aiohttp, "ClientSession", return_value=session):
            result = await tools.webhook(
                "https://discord.com/api/webhooks/123/token | formulario recibido",
                {"author_id": 101},
            )
        self.assertIn("enviado", result.lower())
        self.assertIn("HTTP 204", result)
        self.assertIn("wait=true", session.url)
        self.assertEqual(session.kwargs["json"], {"content": "formulario recibido"})


if __name__ == "__main__":
    unittest.main()
