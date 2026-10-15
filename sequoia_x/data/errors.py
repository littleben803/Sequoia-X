"""数据 Provider 和应用服务的可分类异常。"""


class DataProviderError(RuntimeError):
    """数据源网络、协议或结构异常。"""


class DataNotAvailableError(DataProviderError):
    """请求合法，但源站尚无该日期或证券的数据。"""


class DataIntegrityError(DataProviderError):
    """源数据残缺、格式改变或未通过完整性检查。"""


class CapabilityNotSupportedError(DataProviderError):
    """Provider 不支持所请求的能力或证券范围。"""


class AllProvidersFailedError(DataProviderError):
    """主数据源与所有降级数据源均失败。"""

    def __init__(self, capability: str, errors: list[tuple[str, Exception]]) -> None:
        self.capability = capability
        self.provider_errors = tuple(errors)
        detail = "；".join(f"{name}: {error}" for name, error in errors)
        super().__init__(f"{capability} 的所有数据源均失败：{detail}")
