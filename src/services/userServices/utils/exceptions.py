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


class RegistrationFailedError(UserServiceError):
    def __init__(
        self,
        message: str = "Password could not be verified after registration",
    ):
        self.message = message
        super().__init__(message)
