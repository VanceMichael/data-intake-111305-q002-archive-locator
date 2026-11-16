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

## 目录解释口径（archive_locator/directory.py）

目录库独立于原始归档库，只解释采集信封，不回改原始响应。

- 身份键为 `(tenant, resource_type, id)`。资源 `id` 是唯一来源身份：userName 变更只产生一个带新版本的属性观察，不新建资源；displayName、externalId 相同不触发归并；不同租户的相同 ID/externalId 各自独立。
- 每次观察留存请求属性、实际返回属性与 `meta.version`。请求过但任何观察都未返回的属性是“未读取”；明确返回的空集合（`members: []`）是已确认事实，二者在 `unread_attributes` 与 `group_relationships(members_declared_empty)` 中分别呈现，缺 members 不解释为清空成员。
- `meta.version` 不透明，只做相等比较，不按字符串或词序判断新旧；无版本观察各自独立成切面。同版本观察在一个 facet 内合并，不同版本 facet 互不拼接，增量重访与按需补详情交错时不会出现跨版本的“完整资源”。
- 组成员引用可先于成员详情抵达，详情未读时关系为 pending（`member_detail_missing`），详情抵达后自动复核。`$ref` 与 `value` 矛盾时为 pending（`ref_value_conflict`），display 等名字字段不参与身份补齐；只有来源给出一致的新观察才能消解，待补队列只反映各组最近一次成员观察，历史行保留审计。
- 403 等拒绝进入 access denials：可查询、可回溯到 capture，但不创建、不删除任何资源，也不是成员不存在的证明。
- `fetch_scim_resource` 按需补详情时先归档原始响应再登记观察，目录观察通过 capture_id 与原始响应互相追溯。
