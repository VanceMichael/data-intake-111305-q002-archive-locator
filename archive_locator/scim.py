"""SCIM 目录响应的纯解析助手。

这里只做与存储无关的解释：一次请求实际要求了哪些属性、响应里真正出现了
哪些属性、版本标识是什么，以及成员引用指向哪里。不比较版本先后，也不按
名称匹配身份。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

SCOPE_DECLARED = "declared"
SCOPE_ALL = "all"
SCOPE_ALL_EXCEPT = "all_except"
SCOPE_UNKNOWN = "unknown"

COMMON_ATTRIBUTES = ("id", "externalId", "meta", "schemas")
USER_ATTRIBUTES = (
    "userName", "name", "displayName", "nickName", "profileUrl", "title",
    "userType", "preferredLanguage", "locale", "timezone", "active", "password",
    "emails", "phoneNumbers", "ims", "photos", "addresses", "groups",
    "entitlements", "roles", "x509Certificates",
)
GROUP_ATTRIBUTES = ("displayName", "members")


class ScimError(ValueError):
    """响应不符合本项目使用的 SCIM 目录约定。"""


def inventory(resource_type: str) -> tuple[str, ...]:
    """已知属性清单，仅用于说明“没有请求过”与“请求了但没读到”的差别。"""
    name = str(resource_type).strip().casefold()
    if name.endswith("s"):
        name = name[:-1]
    if name == "user":
        extra = USER_ATTRIBUTES
    elif name == "group":
        extra = GROUP_ATTRIBUTES
    else:
        extra = ()
    return COMMON_ATTRIBUTES + extra


def normalize_attributes(value: Any, field: str) -> tuple[str, ...] | None:
    """把 attributes 参数规范为保序、去重的属性路径元组。

    ``None`` 表示请求方没有声明范围；空元组是“明确要求了零个属性”，与
    ``None`` 含义不同。
    """
    if value is None:
        return None
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ScimError(f"{field} must be a string, an array, or null")
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ScimError(f"{field} entries must be strings")
        for part in item.split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return tuple(result)


def query_projection(url: str) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]:
    """从请求 URL 的 attributes / excludedAttributes 读取投影范围。"""
    if not url:
        return None, None, SCOPE_UNKNOWN
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    lowered = {name.casefold(): values for name, values in query.items()}
    requested = normalize_attributes(lowered.get("attributes"), "attributes")
    excluded = normalize_attributes(lowered.get("excludedattributes"), "excludedAttributes")
    if requested is not None and excluded is not None:
        raise ScimError("attributes and excludedAttributes are mutually exclusive")
    if requested is not None:
        return requested, excluded, SCOPE_DECLARED
    if excluded is not None:
        return requested, excluded, SCOPE_ALL_EXCEPT
    return None, None, SCOPE_ALL


def resource_version(resource: Mapping[str, Any]) -> str | None:
    """返回 meta.version 原值；这是不透明来源标识，不能按字符串排序。"""
    meta = resource.get("meta")
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        raise ScimError("resource.meta must be an object")
    if "version" not in meta:
        return None
    version = meta["version"]
    if not isinstance(version, str) or not version.strip():
        raise ScimError("meta.version must be a non-empty string when present")
    return version


def top_level_attributes(resource: Mapping[str, Any]) -> tuple[str, ...]:
    """响应实际出现的顶层属性名，保留来源顺序。"""
    return tuple(str(name) for name in resource.keys())


def attribute_path(name: str) -> tuple[str, ...]:
    """把 ``name.givenName`` 或完整 URN 形式归一为可匹配的路径。"""
    text = name.strip()
    if text.startswith("urn:") and ":" in text:
        text = text.rsplit(":", 1)[-1]
    return tuple(part for part in text.split(".") if part)


def path_present(resource: Mapping[str, Any], name: str) -> bool:
    """属性路径是否真的出现在这份响应里（缺失不等于空值）。"""
    current: Any = resource
    for part in attribute_path(name):
        if not isinstance(current, Mapping):
            return False
        matched = None
        for key in current.keys():
            if str(key).casefold() == part.casefold():
                matched = key
                break
        if matched is None:
            return False
        current = current[matched]
    return True


def path_value(resource: Mapping[str, Any], name: str) -> Any:
    """取属性路径的值；路径不存在时抛出 ``KeyError``。"""
    current: Any = resource
    for part in attribute_path(name):
        if not isinstance(current, Mapping):
            raise KeyError(name)
        matched = next(
            (key for key in current if str(key).casefold() == part.casefold()), None,
        )
        if matched is None:
            raise KeyError(name)
        current = current[matched]
    return current


def canonical(value: Any) -> str:
    """用于判断同一版本内的两次观察是否一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_error_resource(resource: Mapping[str, Any]) -> bool:
    """SCIM Error 响应即使带了 2xx 也不是资源内容。"""
    schemas = resource.get("schemas")
    if isinstance(schemas, (list, tuple)):
        if any(isinstance(item, str) and item.endswith(":Error") for item in schemas):
            return True
    return False


def error_summary(payload: Any) -> dict[str, Any]:
    """只摘要错误响应中有来源价值的部分，不把它解释成空资源。"""
    if payload is None:
        return {"message": "no response body"}
    if not isinstance(payload, Mapping):
        return {"message": str(payload)[:200]}
    result: dict[str, Any] = {}
    for key in ("schemas", "status", "scimType", "detail", "error", "errorMessage"):
        if key in payload:
            result[key] = payload[key]
    return result or {"message": "error response"}


def parse_member_reference(ref: Any) -> tuple[str, str] | None:
    """从 ``$ref`` 路径取 (资源类型, 资源标识)；无法确定时返回 None。"""
    if not isinstance(ref, str) or not ref.strip():
        return None
    path = unquote(urlsplit(ref.strip()).path)
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]
