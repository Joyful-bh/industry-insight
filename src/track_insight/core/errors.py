class TrackInsightError(RuntimeError):
    pass


class ConfigurationError(TrackInsightError):
    pass


class ModelCallError(TrackInsightError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "model_error",
        diagnostics: dict | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.diagnostics = diagnostics or {}

    def response_metadata(self) -> tuple[str | None, dict, dict | None]:
        responses = [
            self.diagnostics.get("response"),
            self.diagnostics.get("recovery_response"),
            self.diagnostics.get("initial_response"),
            self.diagnostics.get("search_response"),
        ]
        response = next((item for item in responses if isinstance(item, dict)), None)
        request_id = None
        usage: dict = {}
        if response is not None:
            request_id = response.get("request_id") or response.get("id")
            usage = response.get("usage") or {}
        request_id = self.diagnostics.get("request_id") or request_id
        usage = self.diagnostics.get("usage") or usage
        return request_id, usage, self.diagnostics or None


class ContractError(TrackInsightError):
    pass


class PageFetchError(TrackInsightError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
