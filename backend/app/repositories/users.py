from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..exceptions import DuplicateEmailError
from ..models import User


class UserRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email))

    def get_by_id(self, user_id: UUID) -> User | None:
        return self.db.get(User, user_id)

    def create(self, email: str, password_hash: str) -> User:
        user = User(email=email, password_hash=password_hash)
        self.db.add(user)
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            raise DuplicateEmailError from error
        self.db.refresh(user)
        return user
