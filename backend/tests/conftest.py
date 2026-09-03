"""Shared test configuration loaded before application modules."""

import os


os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["DEFAULT_COMPANY_ID"] = "test-company"
os.environ["DEFAULT_USER_ID"] = "test-user"
os.environ["DEFAULT_ROLE"] = "company_admin"

