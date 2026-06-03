class ApiError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, code=None, message=None, status_code=None, issues=None):
        super().__init__(message or code or self.code)
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.message = message
        self.issues = issues


class ValidationApiError(ApiError):
    status_code = 400
    code = "validation_error"


class ProviderNotConfiguredError(ApiError):
    status_code = 503
    code = "provider_not_configured"


class InvalidAuthTokenError(ApiError):
    status_code = 401
    code = "invalid_auth_token"


class LicenseVerificationError(ApiError):
    status_code = 402
    code = "license_verification_failed"
