# Форк QMedia

Это форк [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp) с одним добавленным инструментом. Используется агентом из репозитория [qmedia-ads-agent](https://github.com/qmedia-by/qmedia-ads-agent).

## Зачем форк

Официальный сервер отдаёт три инструмента: `search` (GAQL), `get_resource_metadata`, `list_accessible_customers`. Собрать через них семантику невозможно: `KeywordPlanIdeaService.GenerateKeywordIdeas` — отдельный RPC, а не запрашиваемый через GAQL ресурс. При этом сбор семантики — центральный сценарий нашего агента.

Сторонние MCP-серверы (gomarble и аналоги) умеют keyword planner из коробки, но возвращают зависимость от вендора, от которой проект уходит.

## База

Патч наложен поверх коммита upstream **`91beff7`** (`main`, версия Google Ads API **v24**).

Тегов upstream не публикует, поэтому база фиксируется коммитом, а не тегом — это отличие от исходной формулировки решения в `qmedia-ads-agent/docs/adr/0005`.

## Что добавлено

| Файл | Изменение |
|---|---|
| `ads_mcp/tools/keyword_planning.py` | **новый** — инструмент `generate_keyword_ideas` в namespace `planning` |
| `tests/keyword_planning_test.py` | **новый** — тесты к нему |
| `ads_mcp/tools_config.yaml` | +2 строки: `planning: true` |
| `ads_mcp/config.py` | +1 строка: `"planning"` в `ALL_CATEGORIES` |
| `tests/smoke/golden_tools_list.json` | перегенерирован: +89 строк описания нового инструмента |
| `pyproject.toml` | +1 зависимость: `py-key-value-aio[redis]` — без неё redis-хранилище падает на импорте |
| `ads_mcp/coordinator.py` | +1 аргумент: `enable_cimd=False` — иначе вход через Claude Code не работает, см. ниже |
| `docker/docker-compose-server.yml` | **новый** — серверный стек: `mcp` + `redis` |
| `docker/.env.server.example` | **новый** — шаблон серверного `.env` |
| `.dockerignore` | **новый** — иначе `COPY . .` тащит `.git` внутрь образа |
| `.github/workflows/deployment.yml` | **новый** — деплой на хостинг агентства |
| `.github/workflows/ci.yml` | ветки `main` → `dev`, снят job `llm-tests` (нужен чужой `GEMINI_API_KEY`), добавлен `workflow_call` для гейта деплоя |
| `.gitignore` | +`.venv/` |

Дельта намеренно минимальна: чем меньше тронуто, тем реже конфликты при обновлении upstream. `KeywordPlanService` (прогнозы) и `RecommendationService` не добавляем, пока их не попросят.

Строка в `pyproject.toml` и аргумент в `coordinator.py` — единственные правки upstream-файлов ради развёртывания. Первая же кандидат в upstream-PR: их README рекомендует redis для продакшена, а поставить его нечем. Остальное деплоя касается только новыми файлами.

Инструмент называется `planning_generate_keyword_ideas` — namespace добавляет префикс, как у `customers_`, `search_` и `metadata_`.

## Обновление с upstream

```bash
git fetch upstream
git log --oneline 91beff7..upstream/main    # что изменилось с нашей базы
```

Порядок: смержить новую базу, переналожить патч, обновить SHA в этом файле, **перегенерировать golden-файлы smoke-тестов** — `nox -s smoke_tests` падает, пока `tests/smoke/golden_tools_list.json` не содержит наш инструмент:

```bash
pip install google-genai && python -m tests.smoke.generate_golden
```

Зависимость `google-genai` генератору нужна для импорта, хотя сам golden от неё не зависит; без неё он падает на `ModuleNotFoundError`.

## Локальная разработка

Требуется Python 3.10+ (upstream тестируется на 3.10–3.13).

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e . black
.venv/bin/python -m unittest discover --buffer -s=tests -p "*_test.py"
.venv/bin/python -m unittest tests/smoke/smoke_test.py
.venv/bin/black -l 80 .
```

Ширина строки — 80, как в `noxfile.py`; дефолтные 88 у black дадут расхождение с upstream.

Следить нужно за двумя независимыми осями. Первая — релизы upstream. Вторая — **версия Google Ads API**: мажорная версия живёт около года, после чего запросы к ней блокируются с `UNSUPPORTED_VERSION`. Сейчас в коде v24 (`ads_mcp/utils.py`, `ads_mcp/tools/core.py`, `ads_mcp/tools/keyword_planning.py`, `ads_mcp/resources/discovery.py`). Заведите напоминание за два месяца до sunset — иначе первым признаком проблемы станет отказ у всех менеджеров разом.

## Требования к доступу

Инструмент работает только если developer token имеет permissible use **«Researching keywords and recommendations»**. На уровне доступа Explorer `KeywordPlanService`, `KeywordPlanIdeaService` и `KeywordPlanCampaignService` заблокированы, и код это не обходит.

Для нашего токена (уровень Standard) доступ проверен прямым вызовом REST API и подтверждён.

## Развёртывание

Docker на хостинге агентства, рядом Redis. Google Cloud Run не используется: платёж Google из региона не проходит, поэтому billing account недоступен. Приложению это безразлично.

Сервис живёт на `https://google-ads-mcp.qmedia.by` и выкатывается из GitHub Actions при пуше в `dev`.

### Стек

`docker/docker-compose-server.yml` — два сервиса:

- **mcp** — образ `ghcr.io/qmedia-by/google-ads-mcp:<sha>`, собирается CI. Внутри слушает `8080`, наружу публикуется только на `127.0.0.1:${PORT_MCP}`; домен и TLS даёт reverse-proxy панели ispmanager.
- **redis** — сток `redis:7.2-alpine` с AOF и паролем, на хост не публикуется. Хранит регистрации OAuth-клиентов и токены Менеджеров. Политику вытеснения не задаём намеренно: `maxmemory-policy` начал бы молча выбрасывать токены под нагрузкой — ровно то, ради предотвращения чего Redis здесь и стоит.

Кода на сервере нет, всё внутри образа. Поэтому здесь нет ни выкладки приложения rsync'ом, ни бэкапа, ни отката кода, которые есть в соседнем `influenso`: единственное, что имеет смысл откатывать, — тег образа.

### Хранилище и ключ подписи

Регистрации OAuth-клиентов и токены Менеджеров живут в `client_storage`. Дефолтный бэкенд не переживает перезапуск контейнера, `GOOGLE_ADS_MCP_STORAGE_TYPE=redis` переживает — этим Redis и оправдан.

`GOOGLE_ADS_MCP_JWT_SIGNING_KEY` строго обязательным при этом **не** является, вопреки тому, что можно предположить: не задав его, FastMCP выводит ключ подписи детерминированно из `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` — HKDF с фиксированной солью, `fastmcp/server/auth/jwt_issuer.py`. После перезапуска ключ будет тем же, и токены останутся валидными.

Мы всё равно задаём его явно, и не ради перезапусков: с ним срок жизни токенов не привязан к client secret, и ротация секрета в GCP не разлогинивает весь отдел заодно. Цена — обратная связь: ротация самого ключа разлогинивает всех и обнуляет хранилище, потому что `ads_mcp/auth_storage.py` выводит из него же ключ шифрования. Отдельный `GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY` поэтому не заводим — одним секретом меньше.

### Регистрация клиентов: CIMD выключен

`GoogleProvider` по умолчанию включает **CIMD** (Client ID Metadata Document) — `enable_cimd: bool = True`. Когда он включён, сервер безусловно ставит `client_id_metadata_document_supported: true` в `/.well-known/oauth-authorization-server` (`fastmcp/server/auth/oauth_proxy/proxy.py:2107`), не проверяя, способен ли он этот документ забрать.

Дальше цепочка ломается: Claude Code видит флаг, **пропускает** динамическую регистрацию и подставляет URL-client_id `https://claude.ai/oauth/claude-code-client-metadata`. Сервер должен сам сходить по этому URL — из контейнера это не работает, и `/authorize` отбивает клиента страницей «client ID was not found in the server's client registry». Совет с той страницы («сбросьте токены и переподключитесь») зацикливает: после переподключения клиент прочитает тот же флаг и подставит тот же URL. Настоящая причина уходит в `logger.warning` (`fastmcp/server/auth/cimd.py:745`, строка `CIMD fetch failed`).

Поэтому в `ads_mcp/coordinator.py` передаём `enable_cimd=False`. Флаг уходит из метаданных, клиенты возвращаются к регистрации через `/register`, а она у нас рабочая и переживает перезапуск — регистрации лежат в Redis. Вариант с открытием egress до `claude.ai` отвергнут: он привязывает вход Менеджеров к доступности стороннего домена за Cloudflare. Вернуть CIMD имеет смысл, только если появится клиент, не умеющий динамическую регистрацию, — и тогда egress придётся проверять отдельно.

Разбор целиком — `docs/feedback/2026-08-12-cimd-authorize-failure.md`. Проверено на `fastmcp 3.4.7`; в `pyproject.toml` стоит `fastmcp>=3.2.0`, так что при обновлении FastMCP стоит убедиться, что аргумент не переименован.

### Переменные окружения

Файл `.env` лежит **на уровень выше** `docker/`, в корне `DEPLOYMENT_FOLDER`: CI перезаписывает `docker/` при каждом деплое и снёс бы файл, положенный внутрь. Шаблон — `docker/.env.server.example`. Читает файл только compose (`--env-file`); CI трогает в нём единственную строку `IMAGE_TAG`.

| Переменная | Назначение |
|---|---|
| `PORT_MCP` | хостовый порт для reverse-proxy, `16800`. Должен быть уникален на сервере |
| `IMAGE_TAG` | тег образа; переписывается CI при каждом деплое |
| `REDIS_PASSWORD` | пароль Redis, `openssl rand -hex 16` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | developer token агентства |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | MCC, через который идёт доступ к Аккаунтам |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` | OAuth client (тип Web application) |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` | там же |
| `GOOGLE_ADS_MCP_BASE_URL` | `https://google-ads-mcp.qmedia.by`; Authorized redirect URI = этот адрес плюс `/auth/callback` |
| `GOOGLE_ADS_MCP_JWT_SIGNING_KEY` | ключ подписи токенов, `openssl rand -hex 32`. Технически необязателен, но задаём — см. выше |

Секреты живут только в этом файле на сервере и в репозиторий не попадают.

Чего в файле нет и почему:

- `GOOGLE_ADS_MCP_STORAGE_TYPE`, `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`, имя compose-проекта, репозиторий образа — свойства стека, а не окружения; прибиты в `docker-compose-server.yml`.
- `FASTMCP_HOST` — ни на что не влияет: `ads_mcp/server.py` передаёт `host="0.0.0.0"` явным аргументом.
- `GOOGLE_PROJECT_ID` — остаток инструкции про Cloud Run, кодом не читается.
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` — см. следующий раздел.

### Контроль доступа: что есть на самом деле

Единственный работающий слой — **список Test users** в GCP. OAuth-приложение остаётся в статусе Testing, поэтому Google аннулирует refresh-токены каждые 7 дней, а войти могут только аккаунты из списка. Он же и работает как allowlist Менеджеров.

Второго слоя нет. **Переменной `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` не существует** — ни в этом форке, ни в upstream; она фигурировала в наших ранних документах по ошибке, и для неё в `qmedia-ads-agent` до сих пор лежит генератор `scripts/allowed-customer-ids.mjs`, печатающий значение в никуда. Залогиненный Менеджер может попросить агента сходить в любой Аккаунт, до которого дотягивается MCC агентства, а не только в Аккаунты своих Клиентов. В `.env` переменную не кладём, чтобы не создавать ложного ощущения защиты; реализация — отдельная задача. См. `qmedia-ads-agent/docs/adr/0003`.

### Первый запуск

Порядок жёсткий: ошибка на любом шаге даёт невнятный отказ на первом деплое.

1. **Поддомен и TLS.** Завести `google-ads-mcp.qmedia.by`, выпустить сертификат. Проверка: `curl -I https://google-ads-mcp.qmedia.by` отвечает чем угодно, кроме ошибки TLS.
2. **OAuth-клиент.** В GCP у клиента типа Web application добавить Authorized redirect URI `https://google-ads-mcp.qmedia.by/auth/callback`. Проверка: URI виден в списке после сохранения.
3. **Каталог проекта.** Создать `DEPLOYMENT_FOLDER` на сервере, внутри — пустой `docker/`.
4. **Секреты.** `openssl rand -hex 16` для `REDIS_PASSWORD`, `openssl rand -hex 32` для `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`.
5. **`.env`.** Скопировать `docker/.env.server.example` в `DEPLOYMENT_FOLDER/.env` и заполнить. Проверка: `grep CHANGE_ME .env` ничего не находит.
6. **Reverse-proxy.** Вписать блок из следующего раздела в конфиг сайта в ispmanager.
7. **Секреты GitHub.** В Environment `production`: `DEPLOYMENT_HOST`, `DEPLOYMENT_USER`, `DEPLOYMENT_FOLDER`, `DEPLOYMENT_SSH_PRIVATE_KEY`.

Дальше — пуш в `dev`: соберётся образ, поднимется стек, пройдёт health check.

### Reverse-proxy в ispmanager

MCP по streamable HTTP держит длинные SSE-ответы, и дефолтный конфиг их ломает: буферизация склеивает поток, а `proxy_read_timeout 60s` рвёт его на середине долгого `planning_generate_keyword_ideas`.

```nginx
location / {
    proxy_pass http://127.0.0.1:16800;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

Заголовки `X-Forwarded-*` не нужны: абсолютные URL FastMCP строит из `GOOGLE_ADS_MCP_BASE_URL`, а не из заголовков прокси.

### Деплой и откат

`.github/workflows/deployment.yml`, три блока: `quality` (вызывает `ci.yml`; красный — деплой не идёт) → `image` (сборка и push в GHCR под тегами `<sha>` и `latest`) → `release` (preflight `.env` → rsync `docker/` → `pull` → `up -d --wait` → health check).

Health check — `GET /.well-known/oauth-authorization-server`. Это единственный роут FastMCP, отдающий 200 без авторизации, и появляется он только когда OAuth-провайдер поднялся: 200 доказывает больше, чем открытый TCP-порт. В образе `python:3.11-slim` нет ни `curl`, ни `wget`, поэтому healthcheck контейнера написан на `urllib`.

Автооткат: `release` запоминает текущий `IMAGE_TAG` до переключения и возвращает его, если `up --wait` или health check упали.

Ручной откат на произвольный тег — `workflow_dispatch` с полем `image_tag`. Блоки `quality` и `image` при этом пропускаются: аварийный откат не должен блокироваться красными тестами текущей ветки. То же самое руками:

```bash
cd $DEPLOYMENT_FOLDER
sed -i 's|^IMAGE_TAG=.*|IMAGE_TAG=<sha>|' .env
docker compose --env-file .env -f docker/docker-compose-server.yml up -d
```

### Эксплуатация

```bash
cd $DEPLOYMENT_FOLDER
docker compose --env-file .env -f docker/docker-compose-server.yml ps
docker compose --env-file .env -f docker/docker-compose-server.yml logs -f mcp
```

Логи ротируются драйвером json-file (10 МБ × 3 на сервис): сервер общий, без лимита они пухнут бесконечно. Успешный деплой делает `docker image prune -f` — он трогает только dangling-образы и не может удалить тег, на который откатываются.

**Чего не ловят ни preflight, ни health check.** `GOOGLE_ADS_DEVELOPER_TOKEN` читается лениво, в момент вызова инструмента (`ads_mcp/utils.py`). Контейнер с неверным токеном поднимется, пройдёт health check и будет выглядеть здоровым — сломается только у Менеджера. Preflight ловит лишь отсутствие переменной и незаполненный плейсхолдер.

Ротация `GOOGLE_ADS_MCP_JWT_SIGNING_KEY` — вторая причина внезапного перелогина: она обесценивает все выданные сессии разом. Планируйте её на тихое время.
