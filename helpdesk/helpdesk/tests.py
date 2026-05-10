from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class HealthEndpointTests(TestCase):
    def test_healthz_returns_ok(self):
        response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True})

    def test_readyz_returns_ok_when_database_is_ready(self):
        response = self.client.get(reverse("readyz"))

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True})

    @patch("helpdesk.health.MigrationExecutor.migration_plan", return_value=[object()])
    def test_readyz_reports_pending_migrations(self, _migration_plan):
        response = self.client.get(reverse("readyz"))

        self.assertEqual(response.status_code, 503)
        self.assertJSONEqual(
            response.content,
            {"ok": False, "reason": "pending_migrations"},
        )
