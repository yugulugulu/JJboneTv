class DuplicateEmailError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class ResourceNotFoundError(Exception):
    pass


class DuplicateFolderNameError(Exception):
    pass


class RevisionConflictError(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision


class DomainValidationError(Exception):
    pass


class DuplicateModelKeyError(Exception):
    pass


class DuplicateModelGroupNameError(Exception):
    pass


class ArchivedModelError(Exception):
    pass


class AccountDisabledError(Exception):
    pass


class ModelInUseError(Exception):
    pass


class ModelGroupNotEmptyError(Exception):
    pass


class ModelGroupNotActiveError(Exception):
    pass


class ModelKindMismatchError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass
