from ..core.security import hash_password, verify_password
from ..exceptions import DuplicateEmailError, InvalidCredentialsError
from ..models import User
from ..repositories.users import UserRepository


class AuthService:
    def __init__(self, users: UserRepository):
        self.users = users

    def register(self, email: str, password: str) -> User:
        if self.users.get_by_email(email) is not None:
            raise DuplicateEmailError
        return self.users.create(email=email, password_hash=hash_password(password))

    def login(self, email: str, password: str) -> User:
        user = self.users.get_by_email(email)
        if (
            user is None
            or not user.is_active
            or not verify_password(password, user.password_hash)
        ):
            raise InvalidCredentialsError
        return user
