import json
import unittest
from unittest.mock import AsyncMock

from bot.services.audit import audit_messages, override_name
from bot.services.engine import Engine


class AuditFormattingTests(unittest.TestCase):
    def test_staff_players_and_roles_are_mentions(self):
        text = audit_messages(
            "waitlist registration", 100, 200, {"status": "pending", "country": 300, "club": 400}
        )[0]
        self.assertIn("Staff: <@100>", text)
        self.assertIn("Player: <@200>", text)
        self.assertIn("**Country:** <@&300>", text)
        self.assertIn("**Club:** <@&400>", text)
        self.assertNotIn("{", text)

    def test_setup_role_lists_and_unaffiliated(self):
        text = audit_messages(
            "configuration saved",
            None,
            None,
            {"countries": [1, 2], "clubs": [], "club_limit": 5, "club": None, "opt_in": 3},
        )[0]
        self.assertIn("Staff: System", text)
        self.assertIn("**Countries:** <@&1>, <@&2>", text)
        self.assertIn("**Club limit:** 5", text)
        self.assertIn("**Club:** Unaffiliated", text)
        self.assertIn("**Opt-in role:** <@&3>", text)

    def test_nested_capacity_details_preserve_count(self):
        text = audit_messages(
            "capacity",
            100,
            200,
            {"violations": [{"country": 300, "club": 400, "count": 6, "limit": 5}], "operation": 9},
        )[0]
        self.assertIn("Country: <@&300>", text)
        self.assertIn("Club: <@&400>", text)
        self.assertIn("Count: 6", text)
        self.assertNotIn("Operation", text)
        self.assertNotIn("#9", text)

    def test_large_role_lists_split_without_losing_mentions(self):
        roles = list(range(100000000000000000, 100000000000000200))
        chunks = audit_messages("configuration saved", 100, None, {"countries": roles})
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1900)
        for rid in roles:
            self.assertEqual("\n".join(chunks).count(f"<@&{rid}>"), 1)

    def test_reroll_override_is_labelled_correctly(self):
        text = audit_messages("respin", 100, 200, {"override": "respin_limit"})[0]
        self.assertIn("**Override:** reroll limit", text)
        self.assertEqual(
            override_name("club_capacity,respin_limit"), "club capacity + reroll limit"
        )


class AuditStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_keeps_ids_in_database_but_queues_readable_mentions(self):
        con = AsyncMock()
        con.fetchval.return_value = 50
        con.fetchrow.return_value = {"logs_channel_id": 99}
        engine = Engine(None, None)
        engine.outbox = AsyncMock()
        await engine.audit(con, 10, "manual", 100, 200, country=300, previous=400)
        stored = json.loads(con.fetchval.call_args.args[-1])
        self.assertEqual(stored, {"country": 300, "previous": 400})
        body = engine.outbox.call_args.args[-1]
        for mention in ("<@100>", "<@200>", "<@&300>", "<@&400>"):
            self.assertIn(mention, body)
