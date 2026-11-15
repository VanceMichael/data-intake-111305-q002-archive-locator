# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `tenant`、`resource_type`、`id`、`observation`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `partial_projection`：列表省略members；业务侧关注未读取与明确空集合不同。
- `version_facets`：补详情与增量观察版本不同；业务侧关注不拼出虚假的完整资源。
- `reference_conflict`：成员value与引用冲突且详情403；业务侧关注待核而非猜身份或删除。

另外提供 `/scim/v2/Users/u1`、`/scim/v2/Users/u9`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。

## 领域接入契约

记录外层 tenant、requested_attributes、observation 是本项目采集信封，resource 内层遵循 SCIM 资源结构。attributes 和 excludedAttributes 的投影语义参考 RFC 7644；meta.version 为不透明值。只读取目录，不创建或修改账户，也不计算访问授权。所有用户名均为合成值。

## 目录覆盖视图

`archive_locator.directory.Directory` 在原始采集之上建立只追加的目录查询观察。它不是来源状态的快照，而是对每次查询的覆盖说明。

- **身份**：`(tenant, resource_type, id)`。`userName`、`displayName`、`externalId` 不是身份；改名不会新增资源，不同租户中的同名或相同 `externalId` 也不会自动归并。
- **查询记录**：每次接入保留请求了哪些属性（`requested` 或 `excluded`）、响应顶层实际出现哪些属性（`present`）、响应状态、URL 与时间。状态的 2xx/非 2xx 与 SCIM Error 结构都保留。
- **版本 facet**：成功响应按 `meta.version` 分组。`meta.version` 是不透明标识，不作字符串排序，也没有“最新”推断。缺少 `meta.version` 时每次观察单独成 facet，因为无法证明它们属于同一来源状态。
- **覆盖范围**：同一 facet 内属性取值一致才算 `confirmed_attributes`；取值不一致进入 `conflicted_attributes` 并给出各候选及来源查询。请求过但响应中没有的属性、以及根据投影范围应返回但缺失的属性进入 `unread_attributes`，并区分 `requested_but_absent`、`not_returned`、`not_requested`、`request_scope_unknown`。属性读取从不跨 facet 拼接。
- **未成功响应**：非 2xx 和 SCIM Error 响应记为 `attempts`，效果是 `unread_attempt`；它们不证明资源被删除，也不构成空集合。
- **成员关系**：`relations()` 只解释一个 Group facet。`members` 缺失表示未读取，`members: []` 才表示已观察到的空集合。成员的 `$ref` 与 `value` 指向不同资源时状态为 `conflict`，只列出候选而不选边；成员详情尚未读取时为 `pending_target`；`display` 等名称从不用于推断身份。`pending_relations()` 汇总需要补详情的成员与成员属性本身未读取的 Group facet。

`ingest_response()` 接入一次 SCIM HTTP 响应（列表或详情），`fetch_detail()` 可按需发起详情查询并把原始响应同时写入 `Archive`；详情是否与既有观察同版本由响应的 `meta.version` 决定。并发到达的增量观察与补详情各自写入自己的 facet，读取始终在单一 facet 内完成。
