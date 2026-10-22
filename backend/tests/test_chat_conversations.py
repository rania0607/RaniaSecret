import os
import sys
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

for key, value in {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_NAME": "test",
}.items():
    os.environ[key] = value

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as chatbot
from rag_engine import build_augmented_prompt


class FakeCursor:
    def __init__(self, rows=None, rowcount=1, lastrowid=41):
        self.rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def executemany(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = chatbot.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["user_tier"] = "standard"

    def test_conversation_routes_require_login(self):
        client = chatbot.app.test_client()
        for response in (
            client.get("/api/conversations"),
            client.post("/api/conversations"),
            client.get("/api/conversations/41"),
            client.patch("/api/conversations/41", json={"title": "New"}),
            client.delete("/api/conversations/41"),
        ):
            self.assertEqual(response.status_code, 401)

    def test_list_is_scoped_to_signed_in_user(self):
        row = {"id": 41, "title": "My trip"}
        cursor = FakeCursor([row])
        connection = FakeConnection(cursor)
        with patch.object(chatbot, "get_db", return_value=connection):
            response = self.client.get("/api/conversations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["conversations"], [row])
        self.assertIn("WHERE user_id = %s", cursor.sql)
        self.assertEqual(cursor.params, (7,))
        self.assertTrue(connection.closed)

    def test_conversation_detail_returns_404_when_not_owned(self):
        with patch.object(chatbot, "get_owned_conversation", return_value=None) as owned:
            response = self.client.get("/api/conversations/99")
        self.assertEqual(response.status_code, 404)
        owned.assert_called_once_with(7, 99, include_messages=True)

    def test_context_fetch_is_scoped_and_ordered_oldest_to_newest(self):
        rows = [{"role": "assistant", "content": "new"}, {"role": "user", "content": "old"}]
        cursor = FakeCursor(rows)
        connection = FakeConnection(cursor)
        with patch.object(chatbot, "get_db", return_value=connection):
            history = chatbot.get_conversation_context(7, 41, limit=10)
        self.assertEqual(history, list(reversed(rows)))
        self.assertIn("c.user_id = %s", cursor.sql)
        self.assertEqual(cursor.params, (41, 7, 10))

    def test_guest_transcript_import_is_saved_for_authenticated_user(self):
        cursor = FakeCursor(lastrowid=52)
        connection = FakeConnection(cursor)
        with patch.object(chatbot, "get_db", return_value=connection):
            response = self.client.post(
                "/api/conversations/import",
                json={"messages": [
                    {"role": "user", "content": "  Voyage à Fès  "},
                    {"role": "assistant", "content": "Réponse"},
                ]},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["conversation_id"], 52)
        self.assertEqual(cursor.params, [(52, "user", "Voyage à Fès"), (52, "assistant", "Réponse")])
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_chat_persists_user_before_ai_and_assistant_after(self):
        stored = []
        conversation = {"id": 41}
        rag = {
            "system_prompt": "Use platform data.",
            "user_prompt": "Question",
            "ville_detec": None,
            "intent_detec": None,
        }
        with (
            patch.object(chatbot, "get_user_tier", return_value="standard"),
            patch.object(chatbot, "get_user_limit_data", return_value=(0, date.today())),
            patch.object(chatbot, "create_conversation", return_value=41),
            patch.object(chatbot, "get_owned_conversation", return_value=conversation),
            patch.object(chatbot, "get_conversation_context", return_value=[
                {"role": "user", "content": "Earlier question"},
                {"role": "assistant", "content": "Earlier answer"},
            ]),
            patch.object(chatbot, "store_conversation_message",
                         side_effect=lambda user, conv, role, content: stored.append(role)),
            patch.object(chatbot, "save_message"),
            patch.object(chatbot, "rag_retrieve_and_augment", return_value=rag),
            patch.object(chatbot, "call_groq", return_value="Answer") as call_groq,
        ):
            response = self.client.post("/chat", json={"message": "Question"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["conversation_id"], 41)
        self.assertEqual(stored, ["user", "assistant"])
        call_groq.assert_called_once_with("Use platform data.", "Question", [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ])

    def test_chat_rejects_another_users_conversation(self):
        with patch.object(chatbot, "get_user_tier", return_value="explorer"), \
             patch.object(chatbot, "get_user_limit_data", return_value=(0, date.today())), \
             patch.object(chatbot, "get_owned_conversation", return_value=None):
            response = self.client.post(
                "/chat", json={"message": "Question", "conversation_id": 99}
            )
        self.assertEqual(response.status_code, 404)

    def test_guest_history_is_sent_to_model_without_database_storage(self):
        client = chatbot.app.test_client()
        rag = {"system_prompt": "System", "user_prompt": "Current question"}
        history = [{"role": "user", "content": "Earlier question"}]
        with (
            patch.object(chatbot, "rag_retrieve_and_augment", return_value=rag),
            patch.object(chatbot, "call_groq", return_value="Answer") as call_groq,
        ):
            response = client.post("/chat", json={
                "message": "Current question",
                "history": history,
            })
        self.assertEqual(response.status_code, 200)
        call_groq.assert_called_once_with("System", "Current question", history)

    def test_daily_quota_resets_on_a_new_date(self):
        cursor = FakeCursor([{"msg_count": 49, "last_message_date": date.today() - timedelta(days=1)}])
        connection = FakeConnection(cursor)
        with patch.object(chatbot, "get_db", return_value=connection):
            used, today = chatbot.get_user_limit_data(7)
        self.assertEqual(used, 0)
        self.assertEqual(today, date.today())
        self.assertIn("SET msg_count = 0", cursor.sql)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_explorer_gets_exactly_fifty_successful_requests_per_day(self):
        with self.client.session_transaction() as session:
            session["user_tier"] = "explorer"
        rag = {"system_prompt": "system", "user_prompt": "question"}
        with (
            patch.object(chatbot, "get_user_tier", return_value="explorer"),
            patch.object(chatbot, "get_user_limit_data", return_value=(49, date.today())),
            patch.object(chatbot, "create_conversation", return_value=41),
            patch.object(chatbot, "get_conversation_context", return_value=[]),
            patch.object(chatbot, "store_conversation_message"),
            patch.object(chatbot, "save_message"),
            patch.object(chatbot, "rag_retrieve_and_augment", return_value=rag),
            patch.object(chatbot, "call_groq", return_value="Answer"),
            patch.object(chatbot, "increment_user_message_count"),
        ):
            response = self.client.post("/chat", json={"message": "Last free request"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["msg_count"], 50)
        self.assertEqual(response.get_json()["remaining_queries"], 0)

    def test_explorer_is_blocked_before_model_call_at_limit(self):
        with self.client.session_transaction() as session:
            session["user_tier"] = "explorer"
        with (
            patch.object(chatbot, "get_user_tier", return_value="explorer"),
            patch.object(chatbot, "get_user_limit_data", return_value=(50, date.today())),
            patch.object(chatbot, "call_groq") as call_groq,
        ):
            response = self.client.post("/chat", json={"message": "Over quota"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["limit_reached"])
        call_groq.assert_not_called()

    def test_paid_pack_is_unlimited_and_cannot_be_self_assigned(self):
        rag = {"system_prompt": "system", "user_prompt": "question"}
        with (
            patch.object(chatbot, "get_user_tier", return_value="standard"),
            patch.object(chatbot, "get_user_limit_data") as quota,
            patch.object(chatbot, "create_conversation", return_value=41),
            patch.object(chatbot, "get_conversation_context", return_value=[]),
            patch.object(chatbot, "store_conversation_message"),
            patch.object(chatbot, "save_message"),
            patch.object(chatbot, "rag_retrieve_and_augment", return_value=rag),
            patch.object(chatbot, "call_groq", return_value="Answer"),
        ):
            response = self.client.post("/chat", json={"message": "Unlimited request"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remaining_queries"], "Illimité")
        quota.assert_not_called()
        self.assertEqual(self.client.get("/set-tier/premium").status_code, 403)

    def test_me_reloads_tier_from_database(self):
        with (
            patch.object(chatbot, "get_user_tier", return_value="premium"),
            patch.object(chatbot, "count_today_messages") as count,
        ):
            response = self.client.get("/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["tier"], "premium")
        self.assertEqual(response.get_json()["remaining_queries"], "Illimité")
        count.assert_not_called()

    def test_groq_receives_recent_history_and_low_temperature(self):
        upstream = Mock(status_code=200)
        upstream.json.return_value = {
            "choices": [{"message": {"content": "Answer"}}]
        }
        history = [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]
        with patch.object(chatbot, "GROQ_API_KEY", "test-key"), \
             patch.object(chatbot.requests, "post", return_value=upstream) as post:
            answer = chatbot.call_groq("System", "Current question", history)
        self.assertEqual(answer, "Answer")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": "System"},
            *history,
            {"role": "user", "content": "Current question"},
        ])
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["model"], "openai/gpt-oss-120b")

    def test_system_prompt_does_not_fall_back_to_general_knowledge(self):
        system_prompt, _ = build_augmented_prompt(
            "What are the opening hours?",
            {"ville": None, "data": {}, "all_villes": []},
        )
        self.assertIn("Ne complète pas avec des connaissances générales", system_prompt)
        self.assertIn("N'invente jamais de prix, horaires, adresses", system_prompt)
        self.assertIn("langue de l'utilisateur", system_prompt)


if __name__ == "__main__":
    unittest.main()
