class UserServiceError(Exception):
    """Base for user-service domain errors."""


class InvalidCredentialsError(UserServiceError):
    # Deliberately does not say whether the email or the password was wrong —
    # distinguishing them lets an attacker enumerate registered accounts.
    def __init__(self, message: str = "Invalid email or password"):
        self.message = message
        super().__init__(message)


class EmailAlreadyExistsError(UserServiceError):
    def __init__(self, message: str = "Email already registered"):
        self.message = message
        super().__init__(message)


class PhoneAlreadyExistsError(UserServiceError):
    def __init__(self, message: str = "Phone already registered"):
        self.message = message
        super().__init__(message)


class AddressNotFoundError(UserServiceError):
    # Raised both when the address does not exist and when it belongs to
    # someone else. The caller maps both to 404: answering 403 for another
    # user's address would confirm that a given id exists, which is a fact the
    # caller has no business learning.
    def __init__(self, message: str = "Address not found"):
        self.message = message
        super().__init__(message)


class RegistrationFailedError(UserServiceError):
    def __init__(
        self,
        message: str = "Password could not be verified after registration",
    ):
        self.message = message
        super().__init__(message)
