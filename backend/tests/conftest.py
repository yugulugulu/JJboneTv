import pytest
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import AuthRateLimitEvent


def _clear_auth_rate_limit_events() -> None:
    try:
        with SessionLocal.begin() as db:
            db.execute(delete(AuthRateLimitEvent))
    except SQLAlchemyError:
        # Unit-only test runs may intentionally have no PostgreSQL available.
        # Integration tests will still surface database errors at their actual call site.
        return


@pytest.fixture(autouse=True)
def isolate_auth_rate_limit_events():
    """Keep the production-shared auth bucket isolated between test cases."""
    _clear_auth_rate_limit_events()
    yield
    _clear_auth_rate_limit_events()
