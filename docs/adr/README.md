# Architecture Decision Records

ADR 用来记录会长期影响代码和模块边界的决策。已接受 ADR 不应被静默修改；若决策改变，应新增 ADR 并注明替代关系。

## 状态

```text
Proposed
Accepted
Superseded
Rejected
```

## 命名

```text
NNNN-short-title.md
```

## 模板

```markdown
# ADR NNNN: 标题

- Status: Proposed
- Date: YYYY-MM-DD

## Context

为什么需要做出决策。

## Decision

采用什么方案。

## Consequences

带来的收益、约束和成本。
```

## 当前记录

- [ADR 0001：受限状态机与 Tool Gateway](0001-bounded-runtime-and-tool-gateway.md)

