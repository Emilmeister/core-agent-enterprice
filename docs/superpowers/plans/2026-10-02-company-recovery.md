# Company scope для автоматического восстановления

Проблема подтверждена полным PostgreSQL suite: authenticated app с новым
CORE_AGENT_TENANT_ID выполнил prompt незавершённой cron Task прежней компании.
Scoped get по tenant самой записи проверяет её целостность, но не разрешение
текущего deployment использовать свой model/provider для этой компании.

Цель: authenticated serving process автоматически обрабатывает только company
из trusted Keycloak settings. Чужие записи не claim/lease/expire/reconcile-ятся
и не запускают модель, tool, remote polling, очистку либо push. Generic runtime
без configured recovery tenant сохраняет прежнюю явно доверенную семантику.

Новые env, task state machine, таблицы и broker не добавляются. Используются
существующие stores, SQL predicates и company identifier. Фильтры применяются
до order/cursor/LIMIT рабочих записей и любых side effects, включая cancel-requested rows.
Deadline остаётся абсолютным; другая company не присваивается старым данным.

## Порядок реализации

- [x] Runtime хранит optional trusted `recovery_tenant_id`; composition root
  передаёт `auth_settings.tenant` до старта любой recovery. Это не RunRequest.
- [x] `WorkflowStore.recoverable`, `pending_waits`, `expire_waits` и scheduler
  `recover`, `expire_remote` принимают optional keyword `tenant_id=None`.
  In-memory и PostgreSQL сохраняют одинаковую семантику; scope проверяется
  раньше cancel/unrecoverable/remote веток, lease и claim.
- [x] `WorkspaceCleanupService.recover`, `ChatFileService.sweep` и выбор
  expired upload batches используют тот же scope. Глобальная проверка
  authoritative references перед удалением blob остаётся глобальной.
  Новые rowless upload manifests сохраняют trusted tenant; legacy manifest
  без доказанного tenant не удаляется scoped sweep и требует reconciliation.
- [x] `PostgresTaskStore.reconcile_from_workflows` ограничивает automatic
  projection company до row locks, file lookup и enqueue push.
- [x] Enabled push sender ограничивает claim/config lookup тем же trusted
  tenant до расшифровки credential и сетевой отправки; generic default intact.
- [x] Parent связывает аргументы всех coordinators и обновляет spec, acceptance,
  AGENTS и exact frozen hashes. Файлы исполнителей не пересекаются: parent
  владеет runtime/app, spec/docs/hash и integration test; исполнитель —
  store/service/push implementations и их существующими unit suites.
- [x] Regression доказывает shared PG и разные companies: только собственный
  ready workflow выполняется, чужие records/leases/claims/waits не меняются.
  Primitive tests покрывают фильтр до batch limit и cancel/remote branches.
- [x] Targeted checks, независимое spec review, затем code review; полный
  fresh PostgreSQL/Keycloak suite, ruff, package build и логический commit.

## Проверки

Python/dependencies только через uv. Использовать существующий unittest suite;
не создавать отдельный verifier. Полный canonical suite получает отдельную
disposable test database. Это исправление company boundary не отменяет
открытые target AppArmor и live-provider release gates.

Targeted real PostgreSQL checks: 140 tests, exit0, no skips
(`.local-evidence/company-selectors-reviewed-targeted/`). Последовательность
shared-company root/pool restart → legacy artifact recovery → cleanup API
проверена отдельно: 28 tests, exit0 (`.local-evidence/company-fixture-workflow-targeted/`).
Fixture после assertions завершает только собственные admissions через existing
workflow store и сохраняет reference graph. Read-only повторное ревью без
Critical/Important. UI npm ci/typecheck/build и uv build --no-sources — exit0.
Полный fresh PostgreSQL/Keycloak suite: 1653 tests за 261.352 секунды,
exit0, три dedicated skips; sync/migration/Ruff/cleanup exit0. Evidence —
`.local-evidence/company-recovery-reviewed-ci-final-closed/`. Ни PostgreSQL,
ни Keycloak не пропущены. Проверенный этап сохраняется отдельным commit;
main merge и production-ready не заявляются.
