"""中国交易所证券代码的保守分类；无法确定时必须保留为 UNKNOWN。"""

from sequoia_x.data.models import Exchange, SecurityType


def exchange_from_tdx(market: str) -> Exchange:
    try:
        return {"sh": Exchange.SSE, "sz": Exchange.SZSE, "bj": Exchange.BSE}[market]
    except KeyError as exc:
        raise ValueError(f"未知通达信市场：{market!r}") from exc


def classify_security(exchange: Exchange, code: str, name: str) -> SecurityType:
    """仅在规则足够明确时分类；原始记录永远不会因 UNKNOWN 被丢弃。"""
    normalized_name = name.upper().replace(" ", "")
    if "REIT" in normalized_name or "基础设施" in name:
        return SecurityType.REIT
    if "转债" in name:
        return SecurityType.CONVERTIBLE_BOND
    if "可交换" in name or "交换债" in name:
        return SecurityType.EXCHANGEABLE_BOND
    if exchange == Exchange.BSE and (code.startswith("810") or "定转" in name):
        return SecurityType.CONVERTIBLE_BOND
    if exchange == Exchange.BSE and code.startswith(("821", "910")):
        return SecurityType.BOND
    if exchange == Exchange.BSE and code.startswith("899"):
        return SecurityType.INDEX
    if exchange == Exchange.BSE and code.startswith(("43", "83", "87", "920")):
        return SecurityType.STOCK
    if exchange == Exchange.SSE:
        if code.startswith(("600", "601", "603", "605", "688", "689")):
            return SecurityType.STOCK
        if code.startswith(
            (
                "510",
                "511",
                "512",
                "513",
                "515",
                "516",
                "517",
                "518",
                "560",
                "561",
                "562",
                "563",
                "588",
            )
        ):
            return SecurityType.ETF
        if code.startswith(("500", "501", "502", "505", "506")):
            return SecurityType.FUND
        if code.startswith(
            (
                "009",
                "010",
                "018",
                "019",
                "020",
                "090",
                "100",
                "102",
                "110",
                "113",
                "118",
                "120",
                "122",
                "127",
                "129",
                "130",
                "132",
                "133",
                "134",
                "135",
                "136",
                "137",
                "138",
                "139",
            )
        ):
            return SecurityType.BOND
        if code.startswith(("000", "880", "881")):
            return SecurityType.INDEX
    if exchange == Exchange.SZSE:
        if code.startswith(("000", "001", "002", "003", "300", "301")):
            return SecurityType.STOCK
        if code.startswith(
            ("159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169")
        ):
            return SecurityType.ETF if code.startswith("159") else SecurityType.LOF
        if code.startswith(
            (
                "100",
                "101",
                "102",
                "106",
                "107",
                "108",
                "109",
                "111",
                "112",
                "114",
                "115",
                "116",
                "117",
                "118",
                "119",
                "123",
                "127",
                "128",
                "131",
                "133",
            )
        ):
            return SecurityType.BOND
        if code.startswith("399"):
            return SecurityType.INDEX
    if "基金" in name or "ETF" in normalized_name:
        return SecurityType.FUND
    if "债" in name:
        return SecurityType.BOND
    return SecurityType.UNKNOWN


def is_a_share_stock(exchange: Exchange, code: str, security_type: SecurityType) -> bool:
    return security_type == SecurityType.STOCK and exchange in {
        Exchange.SSE,
        Exchange.SZSE,
        Exchange.BSE,
    }
