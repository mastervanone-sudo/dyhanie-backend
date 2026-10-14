# Бэкенд «Дыхание» (Telegram Stars)

Мини-сервер для оплаты Premium в Telegram Mini App через **Telegram Stars (XTR)**.

## Что умеет
- `POST /stars/invoice` — валидирует `initData` и создаёт счёт (`createInvoiceLink`, currency `XTR`), возвращает `invoiceLink`.
- `POST /telegram/webhook` — принимает `successful_payment` и активирует Premium (SQLite).
- `GET /premium?initData=...` — статус подписки пользователя.
- `GET /health` — проверка живости.
- `GET /set_webhook?url=...&secret=...` — удобная установка вебхука.

## Установка
```
python -m pip install -r requirements.txt
```

## Переменные окружения
| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен бота от @BotFather (обязательно, только на сервере) |
| `WEBHOOK_SECRET` | секрет заголовка вебхука (рекомендуется) |
| `DB_PATH` | путь к SQLite (по умолчанию `premium.db`) |
| `ALLOW_ORIGIN` | origin мини-аппа для CORS (например `https://mastervanone-sudo.github.io`) |
| `PORT` | порт (по умолчанию 8080) |

## Локальный запуск
```
set BOT_TOKEN=123456:ABC...
set WEBHOOK_SECRET=super_secret
python -m uvicorn app:app --port 8080
```
Проверка: http://localhost:8080/health

## Деплой
Нужен публичный **HTTPS**-хост. Варианты:
- **Render / Railway / Fly.io** — бесплатные тарифы, старт команда: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
- **Свой VPS** — systemd-юнит с `uvicorn` за nginx (HTTPS через Let's Encrypt).

Задай `BOT_TOKEN`, `WEBHOOK_SECRET`, `ALLOW_ORIGIN` в переменных окружения хостинга.

## Вебхук
После деплоя (пример для `https://api.example.com`):
```
GET https://api.example.com/set_webhook?url=https://api.example.com/telegram/webhook&secret=super_secret
```
Telegram начнёт присылать апдейты о платежах на этот адрес.

## Подключение к мини-аппу
В файле `breathe-tma/js/stars.js` укажи адрес бэкенда:
```js
export const API_BASE = 'https://api.example.com';
```
После этого кнопки Premium начнут создавать счёт и открывать оплату Stars.

## Тестовые платежи
В Bot API есть тестовый режим Stars для отладки — проверяй сценарий на тестовом боте, прежде чем включать реальные.

## Безопасность
- Токен бота — **только** на сервере, никогда в клиенте и репозитории.
- Всегда проверяй подпись `initData` (уже реализовано) — иначе подписку можно подделать.
- Вебхук защищён заголовком `X-Telegram-Bot-Api-Secret-Token` (совпадает с `WEBHOOK_SECRET`).
- Настоящий факт оплаты подтверждает только `successful_payment` от Telegram — не доверяй статусу с клиента.
