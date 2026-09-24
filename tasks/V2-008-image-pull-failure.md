# V2-008: ImagePullBackOff 诊断

- Status: Planned
- Phase: V2
- Depends on: V2-003

## 目标

对镜像拉取失败形成只读、可回放的诊断，区分镜像不存在、仓库
认证拒绝和暂时网络/限流问题，不读取或展示 registry 凭据。

## 上下文

- `docs/architecture/v2-diagnosis-expansion.md`
- `docs/architecture/kubernetes-tools-v1.md`
- `docs/architecture/security-boundary.md`

## 架构与实现

- 用已有 Pod Status、Events、Deployment Tool 提取 waiting reason、
  镜像引用、拉取 Event 的受限分类和当前 Pod UID/时间。
  若输出 Schema 缺字段，新增版本化字段与兼容测试，不改变旧响应语义。
- Verifier 分开判断 family-level ImagePullBackOff 与精确 subtype：
  not-found 需要明确未找到信号；auth 需要拒绝信号；timeout/
  backoff 不自动等于网络根因。缺具体信号时只报告可证明的状态。
- `imagePullSecrets` 只记录引用是否存在、名称经敏感清洗后的
  元数据；不调用 Secret get，不读取 token 或 Docker config。
- Case v2 包含成功拉取反证、Event 过期/属旧 Pod、认证拒绝、
  镜像不存在和空 Event 的 Partial 分支。

## 测试

- 正常、404/未找到、403/认证、timeout/429、旧 Pod Event、
  Secret 引用与敏感文本注入。
- 输出/审计/API 不含 registry 凭据或原始错误正文。
- 同一初始启动失败症状可依 waiting reason 选择 pull Events，
  而非继续读无 previous instance 的日志。

## 不做

- 不试探 registry 凭据、不拉镜像、不修改 ServiceAccount。
- 不把所有 BackOff 统一解释为 image tag 错误。

## 验收条件

- [ ] ImagePullBackOff 子因由确定性 Evidence 区分。
- [ ] Secret 值完全不读，缺证据/旧事件只给 Partial。
- [ ] Replay、安全与旧 Kubernetes Tool 回归通过。
- [ ] 默认 pytest、strict mypy 与 STATUS 更新通过。
