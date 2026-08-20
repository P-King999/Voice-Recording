from unittest import TestCase, main

from config import dashboard_access_mode


class DashboardAccessModeTests(TestCase):
    def test_missing_password_always_requires_initial_setup(self) -> None:
        self.assertEqual(dashboard_access_mode("", "127.0.0.1", True), "setup")
        self.assertEqual(dashboard_access_mode("", "0.0.0.0", True), "setup")

    def test_local_only_can_bypass_password_when_enabled(self) -> None:
        self.assertEqual(dashboard_access_mode("secret", "127.0.0.1", True), "unprotected")
        self.assertEqual(dashboard_access_mode("secret", "127.0.0.1", False), "login")

    def test_lan_access_always_requires_a_password(self) -> None:
        self.assertEqual(dashboard_access_mode("secret", "0.0.0.0", True), "login")


if __name__ == "__main__":
    main()
