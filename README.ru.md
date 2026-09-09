🇷🇺 Русский · [🇬🇧 English](README.md)

# Telegram MCP

Самостоятельный MCP-сервис для Telegram. Устанавливается на свой компьютер или сервер,
подключается к MCP-клиенту. AI Kit и центральная платформа ZAI для запуска не нужны.
Нужны собственные учётные данные и доступ к API провайдера.

## Установка

Нужны Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) и Python 3.12–3.14.

```sh
git clone https://github.com/zai-one/telegram-mcp.git
cd telegram-mcp
uv sync --frozen
uv run --frozen python scripts/configure.py
uv run --frozen zai-telegram-mcp --config mcp.local.json --check-config
uv run --frozen zai-telegram-mcp --config mcp.local.json
```

Настройщик сохраняет данные только локально. `--check-config` проверяет локальную
конфигурацию без запросов к провайдеру. Последняя команда ждёт подключения MCP-клиента.
Telegram предлагает локальный вход для создания сессии. Passbolt требует GnuPG
и подготовленную конфигурацию своего хранилища.

Инструкции подключения и установки пакета: [INSTALL.md](INSTALL.md).
Функции версии и ограничения: [README.md](README.md), [конфигурация](docs/RUNTIME.md).

## Обратная связь и лицензия

Пользуйтесь и ставьте ⭐, если проект помогает. Ошибки и пожелания присылайте через
[Issues](https://github.com/zai-one/telegram-mcp/issues/new/choose). Я работаю над проектом
и вношу принятые доработки здесь; поддержка не гарантируется.

Лицензия [LicenseRef-ZAI-ONE](LICENSE) разрешает установку и использование для своих
аккаунтов. Она не является открытой лицензией и не разрешает распространение
производных продуктов. Права сторонних компонентов сохранены в [NOTICE](NOTICE).
Токены, сессии, ключи и данные аккаунтов в Issues не отправляйте.
