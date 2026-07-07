"""AuthService behavior tests."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.account import Account
from app.models.cookie_pool import CookiePool
from app.services.auth_service import AuthService


@pytest.fixture
def auth_db():
    """SQLite DB with only the tables AuthService needs."""
    engine = create_engine("sqlite:///:memory:")
    Account.__table__.create(engine)
    CookiePool.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.mark.asyncio
async def test_login_invalid_encrypted_password_returns_key_mismatch(auth_db):
    account = Account(email="old@example.com", password_encrypted="not-a-fernet-token")
    auth_db.add(account)
    auth_db.commit()

    service = AuthService(auth_db)
    success, cookie_value = await service.login(account)

    assert success is False
    assert cookie_value is None
    assert "ENCRYPTION_KEY" in service.last_error
    assert account.login_fail_count == 1


@pytest.mark.asyncio
async def test_refresh_cookie_updates_account_refresh_time(auth_db):
    account = Account(email="fresh@example.com", password_encrypted="unused")
    auth_db.add(account)
    auth_db.commit()

    service = AuthService(auth_db)

    async def fake_login(_account):
        return True, "PHPSESSID=abc"

    service.login = fake_login

    cookie = await service.refresh_cookie_for_account(account)

    assert cookie is not None
    assert cookie.name == "auto_fresh@example.com"
    assert account.last_cookie_refresh_at is not None
    assert isinstance(account.last_cookie_refresh_at, datetime)
