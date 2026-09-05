"""PG-first 命令服务（阶段三第五步，增量实现中）：每个命令 = 单个数据库事务。

模板（docs/PG_FIRST_SERVICE.md §5）：事务内读最新事实（Row）→ rules 纯函数校验
（权限/租户/版本 CAS/状态机，稳定错误码）→ 写业务行 + 幂等记录 + 追加审计 → 提交后返回
领域对象；异常 → 整事务回滚（无部分提交）。本模块只编排（repository 原语 + rules），
不复制领域规则；Repository 可插拔（Postgres=生产语义；Memory=单测/契约后端）。

已实现命令子集（本回合）：create_ticket / submit / approve / reject；
后续命令（create_refund_draft / execute / reconcile / close_ticket）将按同模板补齐。
"""
from __future__ import annotations

import json
from dataclasses import replace

from src.domain.after_sales.models import (
    AfterSalesError,
    AfterSalesErrorCode,
    AfterSalesTicket,
    Operation,
    OperationStatus,
    Role,
    TicketStatus,
)
from src.domain.after_sales.policies import detect_conflict, match_policies
from src.domain.after_sales.rules import (
    check_operation_transition,
    require_role,
    validate_customer_order_match,
    validate_decision_version,
    validate_order_access,
)
from src.domain.idempotency import payload_hash as _payload_hash
from src.persistence.pg_backed import (
    order_from_row,
    operation_from_row,
    policy_from_row,
    ticket_from_row,
)
from src.repo import AfterSalesRepository, AuditRow, IdemRow, TicketRow


def _fkey(tenant_id: str, raw_key: str) -> str:
    """幂等规范键（D2：租户前缀作用域）。"""
    return f"{tenant_id}:{raw_key}"


def _phash_ticket(cmd) -> str:
    return _payload_hash({
        "tenant_id": cmd.tenant_id, "order_id": cmd.order_id,
        "customer_id": cmd.customer_id, "request_type": cmd.request_type.value,
        "reason": cmd.reason, "reason_tags": list(cmd.reason_tags),
    })


class PgCommandService:
    """PostgreSQL-first 命令服务（单事务八命令；本回合子集：建单/提交/审批/拒绝）。"""

    def __init__(self, repo: AfterSalesRepository):
        self._repo = repo

    def _audit(self, tenant_id, action, entity_type, entity_id, actor: Role,
               before, after, note=None, idem_key=None) -> None:
        self._repo.insert_audit(AuditRow(
            tenant_id, action, entity_type, entity_id, actor.value,
            before.value if before is not None else None,
            after.value if after is not None else None, idem_key, note))

    @staticmethod
    def _json_tags(tags) -> str:
        return json.dumps(list(tags), ensure_ascii=False)

    def _load_op(self, tenant_id: str, operation_id: str):
        row = self._repo.get_operation(tenant_id, operation_id)
        if row is None:
            raise AfterSalesError(AfterSalesErrorCode.OPERATION_NOT_FOUND,
                                  f"操作 {operation_id} 不存在")
        return row

    def _match_policy(self, tenant_id, request_type, reason_tags, days_since_sign):
        policies = [policy_from_row(r) for r in self._repo.list_policies()]
        matched = match_policies(policies, tenant_id, request_type, reason_tags,
                                 days_since_sign)
        if not matched:
            raise AfterSalesError(AfterSalesErrorCode.POLICY_NOT_FOUND,
                                  f"无适用政策（tenant={tenant_id}, tags={reason_tags}），证据不足，建议转人工")
        if detect_conflict(matched) is not None:
            raise AfterSalesError(AfterSalesErrorCode.POLICY_CONFLICT,
                                  f"多条适用政策退款比例不一致（{len(matched)} 条），冲突需转人工")
        return matched[0]

    def create_ticket(self, cmd) -> AfterSalesTicket:
        """事务内：权限→订单行锁+资格→（CUSTOMER）客户-订单匹配→政策命中→next_seq→工单+幂等+审计。"""
        require_role(cmd.actor, (Role.CUSTOMER, Role.AGENT), "只有客户或 Agent 可以创建工单")
        if not cmd.reason or not cmd.reason.strip():
            raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD, "缺少售后理由")
        if not cmd.reason_tags:
            raise AfterSalesError(AfterSalesErrorCode.MISSING_REQUIRED_FIELD,
                                  "缺少诉求标签（无法匹配政策证据）")
        fkey = _fkey(cmd.tenant_id, cmd.idempotency_key)
        phash = _phash_ticket(cmd)
        with self._repo.unit_of_work():
            order_row = self._repo.lock_order_for_update(cmd.tenant_id, cmd.order_id)
            order = order_from_row(order_row) if order_row is not None else None
            validate_order_access(order, cmd.tenant_id)
            if cmd.actor == Role.CUSTOMER:
                validate_customer_order_match(order, cmd.customer_id)
            existing = self._repo.get_idem(cmd.tenant_id, fkey)
            if existing is not None:
                if existing.payload_hash == phash and existing.refund_id != "<pending>":
                    row = self._repo.get_ticket(cmd.tenant_id, existing.refund_id)
                    if row is not None:
                        return ticket_from_row(row)
                raise AfterSalesError(AfterSalesErrorCode.IDEMPOTENCY_CONFLICT,
                                      "同键异载荷：工单创建被拒绝")
            self._match_policy(cmd.tenant_id, cmd.request_type, cmd.reason_tags,
                               order.days_since_sign)
            seq = self._repo.next_seq(cmd.tenant_id, "ticket")
            ticket_id = f"TKT-{seq:05d}"
            row = TicketRow(cmd.tenant_id, ticket_id, cmd.order_id, cmd.customer_id,
                            cmd.request_type.value, cmd.reason, "open", None, 1,
                            cmd.actor.value, self._json_tags(cmd.reason_tags))
            self._repo.insert_ticket(row)
            self._repo.insert_idem(IdemRow(cmd.tenant_id, fkey, phash, ticket_id))
            self._audit(cmd.tenant_id, "create_ticket", "ticket", ticket_id, cmd.actor,
                        None, TicketStatus.OPEN, idem_key=fkey)
        return ticket_from_row(row)

    def submit(self, tenant_id: str, cmd) -> Operation:
        require_role(cmd.actor, (Role.AGENT,), "只有 Agent 可以提交审批")
        with self._repo.unit_of_work():
            row = self._load_op(tenant_id, cmd.operation_id)
            check_operation_transition(OperationStatus(row.status),
                                       OperationStatus.PENDING_APPROVAL)
            target = replace(row, status="pending_approval")
            self._repo.update_operation_versioned(target, expected_version=row.version)
            final_row = self._repo.get_operation(tenant_id, cmd.operation_id)
            self._audit(tenant_id, "submit", "operation", cmd.operation_id, cmd.actor,
                        OperationStatus(row.status), OperationStatus.PENDING_APPROVAL)
        return operation_from_row(final_row)

    def approve(self, tenant_id: str, cmd) -> Operation:
        require_role(cmd.actor, (Role.APPROVER,), "只有授权人员可以审批")
        with self._repo.unit_of_work():
            row = self._load_op(tenant_id, cmd.operation_id)
            validate_decision_version(row.version, cmd.decision_version)
            check_operation_transition(OperationStatus(row.status),
                                       OperationStatus.APPROVED)
            target = replace(row, status="approved", decision_version=cmd.decision_version)
            self._repo.update_operation_versioned(target, expected_version=row.version)
            final_row = self._repo.get_operation(tenant_id, cmd.operation_id)
            self._audit(tenant_id, "approve", "operation", cmd.operation_id, cmd.actor,
                        OperationStatus(row.status), OperationStatus.APPROVED)
        return operation_from_row(final_row)

    def reject(self, tenant_id: str, cmd) -> Operation:
        require_role(cmd.actor, (Role.APPROVER,), "只有授权人员可以审批")
        with self._repo.unit_of_work():
            row = self._load_op(tenant_id, cmd.operation_id)
            validate_decision_version(row.version, cmd.decision_version)
            check_operation_transition(OperationStatus(row.status),
                                       OperationStatus.REJECTED)
            target = replace(row, status="rejected")
            self._repo.update_operation_versioned(target, expected_version=row.version)
            final_row = self._repo.get_operation(tenant_id, cmd.operation_id)
            self._audit(tenant_id, "reject", "operation", cmd.operation_id, cmd.actor,
                        OperationStatus(row.status), OperationStatus.REJECTED,
                        note=cmd.reason)
        return operation_from_row(final_row)
