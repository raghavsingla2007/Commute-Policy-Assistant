"""Fixed user-facing strings; the model never writes these."""

AUTH_ERROR = "Your session is invalid or has expired. Please sign in again."
NOT_COVERED = ("I couldn't find an answer to this in your company's transport policy. "
               "Please contact your transport desk for help.")
UNAVAILABLE = "We couldn't get an answer right now. Please try again in a moment."
BAD_QUESTION = "Please send a question as text, up to 500 characters."
BAD_REQUEST = "The request is not valid. Please check it and try again."
FORBIDDEN = "You don't have permission to do this."
OTHER_COMPANY = "You can only upload policies for your own company. Nothing was changed."
PAYLOAD_TOO_LARGE = "The request is too large."
VERSION_CONFLICT = "A newer or equal policy version is already active. Nothing was changed."
INVALID_POLICY = "The policy document could not be read. Nothing was changed."
INGEST_FAILED = "The policy upload failed. The current policy is unchanged."
