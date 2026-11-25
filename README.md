# AeroHub - типова інформаційна система аеропорту

Програмна реалізація ядра ІС «AeroHub» до звіту з комп'ютерного практикуму
«Проектування і розроблення інформаційних систем та технологій», команда №26
(Борисенко Є. В., Сєргєєва К. Р.).

Прототип реалізує розклад і операційну базу даних рейсів (AODB) з історією змін, розподіл ресурсів аеропорту,
табло FIDS, модуль A-CDM (TOBT, TSAT, DPI) і завдання наземного обслуговування. Архітектура відповідає
розділу 10 звіту: мікросервіси з керуванням подіями, база даних на сервіс, Transactional Outbox,
ідемпотентні споживачі, модель для читання (CQRS) для табло.

```
 Браузер (веб-клієнт, табло) -> gateway (nginx) -> flight | resource | fids | acdm | task  -> PostgreSQL (база на сервіс)
 сервіси --outbox--> Apache Kafka (flight.events, resource.allocations, acdm.milestones, task.events) --> сервіси-підписники
 fids-service --WebSocket--> веб-клієнт і табло
```

## Склад

| Компонент | Призначення | Основні технології |
|---|---|---|
| `services/flight_service` | AODB: рейси, статуси, історія змін, імпорт розкладу SSIM, демо-автентифікація | FastAPI, SQLAlchemy 2, PostgreSQL |
| `services/resource_service` | RMS: стоянки, гейти, стійки, стрічки; автоматичний і ручний розподіл; конфлікти | FastAPI, PostgreSQL `EXCLUDE USING gist` |
| `services/fids_service` | Модель читання табло, публічний API статусу рейсу, WebSocket-оновлення | FastAPI, WebSocket |
| `services/acdm_service` | Етапи A-CDM, TOBT, секвенсор TSAT, повідомлення DPI | FastAPI, SQLAlchemy 2 |
| `services/task_service` | Завдання наземного обслуговування за шаблонами, контроль SLA | FastAPI, SQLAlchemy 2 |
| `libs/aerohub_common` | Конверт події, шина Kafka / in-memory, outbox, міграції, JWT і ролі | aiokafka, PyJWT |
| `web/` | Веб-клієнт (`index.html`) і табло (`board.html`) | HTML, CSS, JavaScript без збирання |
| `gateway/` | API Gateway: маршрутизація, ліміти, заголовки безпеки, WebSocket | nginx |

## Запуск

Потрібен Docker з Docker Compose.

```bash
cp .env.example .env        # задайте власний JWT_SECRET
docker compose up -d --build
```

- Веб-клієнт: <http://localhost:8080> (користувачі `planner`, `dispatcher`, `agent`, `handler`, `admin`; пароль = логін)
- Табло FIDS: <http://localhost:8080/board.html>
- Swagger API: <http://localhost:8001/docs> (рейси), `:8002` (ресурси), `:8003` (FIDS), `:8004` (A-CDM), `:8005` (завдання)

Демо-розклад на два тижні: вкладка «Розклад (SSIM)» -> файл `docs/demo_schedule.ssim`.

Оновлення до нової версії: `docker compose up -d --build`. Бази даних нових сервісів і міграції схеми
застосовуються автоматично під час старту.

## Тести

```bash
pip install -r services/requirements-dev.txt -e libs/aerohub_common
scripts/test-db.sh          # тестова PostgreSQL на порту 55432
scripts/test.sh             # 41 модульний тест п'яти сервісів з покриттям

docker compose up -d --build
AEROHUB_E2E=1 pytest tests/e2e -q   # 3 наскрізні тести через шлюз і справжню Kafka
python tests/pilot/pilot.py          # сценарій дослідної експлуатації (навантаження, узгодженість)
```

## Документи

- `docs/API.md` - REST API і події Kafka
- `docs/defects.md` - журнал дефектів D-01 - D-15 (тестування і дослідна експлуатація)
- `docs/screenshots/` - знімки екрана веб-клієнта і табло

## Безпека і оновлення

- Образи збираються з зафіксованих версій `services/requirements.lock`, процеси працюють від непривілейованого користувача.
- CI (`.github/workflows/ci.yml`): модульні тести, `pip-audit`, збирання образів на кожен push і pull request.
- Dependabot: pip і Docker щотижня, GitHub Actions щомісяця.

## Обмеження прототипу

- Автентифікацію виконує службовий ендпоінт з демо-користувачами замість Keycloak; формат JWT той самий.
- Час у розкладі SSIM трактується як UTC; парсер обробляє лише записи типу 3 без варіантів маршрутів.
- Модулі багажу, білінгу, інцидентів, сповіщень та інтеграції з SITA/NM описано в архітектурі звіту і заплановано на реліз 3.
