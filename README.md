# 档案机构 SCIM 目录的属性覆盖与成员接入

这是资料接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。`directory.py` 在原始归档之上提供 SCIM 目录解释：按租户与资源类型组织资源，记录每次查询请求了哪些属性、响应实际包含哪些属性及来源版本，让机构和成员关系能解释自己的覆盖范围。原始接收与业务解释分别建库、独立演进，程序不自动执行门户提交；重复观察分别保留，原始内容按摘要复用。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q archive_locator tests
python3 -m archive_locator demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取、原始归档和目录登记，并输出原样记录与目录中的资源身份；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m archive_locator serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m archive_locator capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## SCIM 目录解释

目录库（独立 SQLite 文件）把采集信封解释成可查询的目录状态，身份键是来源声明的 `(tenant, resource_type, id)`：

- **资源 ID 才是身份**：改 `userName`、同名或相同 `externalId` 都不会新建或归并一人；两个租户即使 ID/externalId 相同也各自独立，目录不做跨租户归并。
- **覆盖范围可解释**：每次观察保留 `requested_attributes`、实际 `returned_attributes`（及该次观察的 `unread_attributes`）与 `meta.version`。曾被请求但从未返回的属性进入未读列表，与明确返回的空集合（如 `members: []`）严格区分——列表缺 members 不会被解释成清空机构成员。
- **版本切面不拼接**：`meta.version` 是不透明来源版本标识，只做相等比较，不按字符串排序。不同版本观察到的属性按版本切面（facet）分别保留，增量重访与按需补详情交错时也不会拼成一份看似完整的资源。
- **成员关系待补**：成员引用可以先于成员详情抵达；详情未读时关系为 pending。`$ref` 与 `value` 矛盾时保持待核，不凭 displayName 等名字补齐身份，只有来源给出一致的新观察才能消解。
- **拒绝不是删除**：403 等权限不足响应登记为 access denial，可追溯但不删除资源，也不消解待核关系。

```sh
# 抓取信封列表：原始响应入归档库，观察逐条入目录库
python3 -m archive_locator directory ingest --base-url http://127.0.0.1:实际端口 \
  --archive .runtime/raw.sqlite3 --directory .runtime/scim.sqlite3 --source local-source

# 查看一个资源的完整覆盖范围（版本切面、已确认/未读属性、成员、拒绝记录）
python3 -m archive_locator directory show --directory .runtime/scim.sqlite3 \
  --tenant archive-a --resource-type Groups --id g1

# 待补成员关系（冲突或缺成员详情）
python3 -m archive_locator directory pending --directory .runtime/scim.sqlite3 --tenant archive-a

# 按需补单个资源详情；原始响应同样归档，403 记为拒绝而非删除
python3 -m archive_locator directory fetch --base-url http://127.0.0.1:实际端口 \
  --archive .runtime/raw.sqlite3 --directory .runtime/scim.sqlite3 \
  --tenant archive-a --resource-type Users --id u1 --attributes id,userName
```

库内查询对应 `Directory.resource_view`、`confirmed_attributes`、`unread_attributes`、`facets`、`group_relationships`、`pending_relationships` 与 `access_denials`，三个业务交错情况的口径见 `tests/test_directory.py` 与 `fixtures/business_cases.json`。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`，目录解释规则见 `tests/test_directory.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `archive_locator` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m archive_locator demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
