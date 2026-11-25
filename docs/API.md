# REST API прототипу AeroHub

Усі запити йдуть через шлюз `http://localhost:8080`. Захищені ендпоінти потребують заголовка
`Authorization: Bearer <token>` (токен: `POST /api/v1/auth/token`).

## Flight Service
| Метод | Шлях | Ролі | Опис |
|---|---|---|---|
| POST | /api/v1/auth/token | - | Отримати JWT (демо-користувачі) |
| GET | /api/v1/auth/me | будь-яка | Поточний користувач і ролі |
| GET | /api/v1/flights?date=&direction=&status=&q= | будь-яка | Список рейсів |
| POST | /api/v1/flights | planner, aoc_dispatcher | Створити рейс |
| GET | /api/v1/flights/{id} | будь-яка | Рейс |
| PATCH | /api/v1/flights/{id} | planner, aoc_dispatcher | Змінити рейс (`version` або `If-Match`; `reason` для часу і типу ПС) |
| POST | /api/v1/flights/{id}/status | aoc, planner, agent, handler | Змінити статус |
| POST | /api/v1/flights/{id}/cancel | planner, aoc_dispatcher | Скасувати рейс |
| DELETE | /api/v1/flights/{id} | planner | Видалити помилково створений рейс |
| POST | /api/v1/flights/{id}/link | planner, aoc_dispatcher | Пов'язати прибуття і відправлення (turnaround) |
| GET | /api/v1/flights/{id}/history | будь-яка | Історія змін |
| POST | /api/v1/schedules/imports | planner | Імпорт файлу SSIM (multipart, поле `file`) |

## Resource Service
| Метод | Шлях | Ролі | Опис |
|---|---|---|---|
| GET | /api/v1/resources?type= | будь-яка | Довідник ресурсів |
| POST | /api/v1/resources | planner | Додати ресурс |
| PATCH | /api/v1/resources/{id} | planner | Змінити ресурс |
| POST | /api/v1/resources/{id}/unavailability | planner, aoc_dispatcher | Закрити ресурс на період і перерозподілити рейси |
| GET | /api/v1/allocations?date= | будь-яка | Призначення на дату |
| GET | /api/v1/allocations/conflicts?date= | будь-яка | Невирішені конфлікти |
| PUT | /api/v1/allocations/{flightId} | planner, aoc_dispatcher | Ручне призначення `{kind, resource_codes}` |
| POST | /api/v1/allocations/auto?date= | planner | Перерахувати розподіл на дату |

## FIDS Service (публічний)
| Метод | Шлях | Опис |
|---|---|---|
| GET | /api/v1/public/flights?direction=&date= | Рядки табло |
| GET | /api/v1/public/flights/{flightNo}?date= | Статус рейсу за номером |
| WS | /ws/fids | Потік оновлень `fids.update`, `fids.remove`, `resource.conflict`, `event` (A-CDM і завдання) |

## A-CDM Service
| Метод | Шлях | Ролі | Опис |
|---|---|---|---|
| GET | /api/v1/acdm/flights?date= | будь-яка | Рейси дня з етапами, TOBT, TSAT |
| GET | /api/v1/acdm/flights/{id} | будь-яка | Рейс з останнім значенням кожного етапу |
| PUT | /api/v1/acdm/flights/{id}/tobt | handler, aoc_dispatcher, agent | Підтвердити TOBT `{tobt}` (не раніше SOBT - 15 хв) |
| POST | /api/v1/acdm/flights/{id}/milestones | aoc_dispatcher | Ручний етап `{code, value}`: ALDT, ATOT, ASBT, ARDT |
| GET | /api/v1/acdm/flights/{id}/dpi | будь-яка | Журнал DPI (E-DPI, T-DPI-t, T-DPI-s, A-DPI, C-DPI) |

## Task Service
| Метод | Шлях | Ролі | Опис |
|---|---|---|---|
| GET | /api/v1/tasks?date=&team=&status=&overdue= | будь-яка | Завдання наземного обслуговування |
| GET | /api/v1/tasks/templates | будь-яка | Шаблони завдань |
| POST | /api/v1/tasks/{id}/start | handler, aoc_dispatcher | Почати `{version, at?}` |
| POST | /api/v1/tasks/{id}/complete | handler, aoc_dispatcher | Завершити `{version, at?}`; `at` - фактичний час для офлайн-синхронізації |

## Події Kafka
| Топік | Події | Публікує | Споживає |
|---|---|---|---|
| flight.events | FlightCreated, FlightUpdated, FlightStatusChanged, FlightCancelled, FlightDeleted | flight-service | resource-service, fids-service, acdm-service, task-service |
| resource.allocations | ResourceAllocated, ResourceReleased, ResourceConflictDetected | resource-service | fids-service |
| acdm.milestones | MilestoneReached, TobtChanged, TsatIssued | acdm-service | task-service, fids-service |
| task.events | TaskCreated, TaskUpdated, TaskCompleted, TaskOverdue | task-service | acdm-service, fids-service |

Конверт події: `event_id`, `type`, `version`, `occurred_at`, `aggregate_id` (ключ розділу), `correlation_id`, `source`, `payload`.
