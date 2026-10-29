# Stream Monitor Bot — @Alert_Checker_RealTime_bot

Telegram-бот для мониторинга прямых эфиров (vMix → YouTube, Yoda.az и др.).
Работает как мини-SIEM (журнал событий, state machine, корреляция, отчёты)
и мини-firewall (whitelist, rate limit, защита от SSRF).

## Возможности

| Уровень | Что проверяется |
|---|---|
| Сеть | DNS и смена IP хоста, TCP-подключение и задержка, HTTP-статус, редиректы на неизвестные домены, TLS: срок действия, смена сертификата и издателя |
| HLS | свежесть плейлиста (растёт ли media sequence), `#EXT-X-ENDLIST`, `#EXT-X-GAP`, задержка эфира по PROGRAM-DATE-TIME, скорость загрузки и реальный битрейт последнего сегмента |
| Поток | ffprobe: кодек, разрешение, FPS, битрейт, наличие аудио |
| Картинка/звук | ffmpeg-сэмпл 4 с: `blackdetect`, `freezedetect`, `silencedetect`, `volumedetect` (раз в N проверок, через общий семафор) |
| Платформы | yt-dlp достаёт прямую ссылку со страниц YouTube и др. и проверяет, идёт ли эфир сейчас |
| Данные платформы | YouTube и Kick (через API канала): идёт ли эфир, сколько зрителей сейчас, название эфира, время старта, канал и число подписчиков, категория, лайки. Видны в `/status`, `/check` и в часовом статусе (👁) |
| Сайты-агрегаторы | плеер в iframe (canlitv.com и т.п.): бот заходит в плеер, находит источник (JW Player, hls.js, YouTube embed) и пишет, какой именно источник не отвечает. Сайты, которые встраивают YouTube-эфир (com.com.tr), проверяются по самому эфиру на YouTube. Если сайт закрыт Cloudflare-проверкой, бот пишет «проверить невозможно» (🟡), а не «эфир упал» |
| Сервер | CPU, RAM, диск, сеть (psutil), vMix HTTP API: streaming, recording, активный input |

**Состояния эфира:** `UNKNOWN → UP ⇄ DEGRADED → DOWN → RECOVERED → UP`
- DOWN выставляется только после `FAIL_THRESHOLD` (3) неудач подряд (антифлаппинг)
- напоминание раз в 5 минут, пока проблема не решена
- если за `CORRELATION_WINDOW_SEC` упало несколько эфиров, приходит один общий алерт; бот при этом проверяет интернет на своём сервере и статус vMix
- при восстановлении приходит 🟢 RECOVERED с длительностью простоя
- в `DAILY_REPORT_TIME` бот присылает ежедневный отчёт: аптайм %, число падений, среднее время восстановления

## Установка

### 1. ffmpeg / ffprobe
- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **Windows:** `winget install Gyan.FFmpeg` (или скачайте сборку с https://www.gyan.dev/ffmpeg/builds/ и добавьте `bin` в PATH)
- **macOS:** `brew install ffmpeg`

Если ffmpeg лежит не в PATH, укажите путь в `.env`: `FFPROBE_PATH=...`, `FFMPEG_PATH=...`.

### 2. Python-зависимости (Python 3.11+)
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Настройка `.env`
```bash
cp .env.example .env
chmod 600 .env
```
| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `ALLOWED_USER_IDS` | ID пользователей через запятую. Если пусто, бот на `/start` сообщает ваш user_id |
| `VMIX_HOST` | IP машины с vMix (опционально, порт 8088) |
| `DAILY_REPORT_TIME` | время ежедневного отчёта, `HH:MM` |

| `ALERT_MIN_SEVERITY` | какие события бот присылает сам: `CRITICAL` — только красные (по умолчанию), `WARNING` — ещё и жёлтые |
| `SEND_RECOVERED` | присылать 🟢 RECOVERED после красного алерта (`true`) |
| `STATUS_EVERY_MIN` | статус всех эфиров раз в N минут (по умолчанию 60, `0` — выключить) |

Все пороги и таймауты (`MIN_VIDEO_BITRATE_KBPS`, `MIN_FPS`, `FAIL_THRESHOLD`,
`SILENCE_NOISE_DB`, `TIMEOUT_FFPROBE` …) задаются в `.env` и описаны в [config.py](config.py).

### 4. Запуск
```bash
python main.py
```
Первый запуск:
1. Напишите боту `/start`: он пришлёт ваш user_id.
2. Добавьте этот ID в `ALLOWED_USER_IDS` и перезапустите бота.

## Команды

| Команда | Действие |
|---|---|
| `/add <url> <имя>` | добавить эфир |
| `/remove <имя>` | убрать эфир |
| `/list` | эфиры и статусы 🟢/🟡/🔴 |
| `/check <url>` | разовая полная диагностика (или просто пришлите ссылку) |
| `/status <имя>` | подробный отчёт |
| `/interval <сек>` | интервал проверки 5–60 (по умолчанию 10) |
| `/events [N]` | журнал событий (SIEM) |
| `/report` | сводка за 24 часа |
| `/mute <имя> <мин>` | отключить алерты на время плановых работ (`0` снимает mute) |

> Для RTMP/SRT указывайте **ссылку воспроизведения**, а не ingest-адрес публикации
> (например, `rtmp://a.rtmp.youtube.com/live2/<key>` прочитать нельзя).

## Безопасность
- Токен читается только в `config.py` из окружения. В логах он маскируется (`***`) фильтром и форматтером.
- Whitelist пользователей. Попытки доступа посторонних пишутся в журнал (WARNING).
- Rate limit: 20 команд в минуту, `/check` не чаще раза в 15 с.
- Разрешены только схемы `http/https/rtmp/rtmps/srt`. Хосты, которые резолвятся в приватные адреса или localhost, запрещены (SSRF); исключение — `VMIX_HOST`. Каждый редирект проверяется заново.
- На всех сетевых операциях и процессах ffmpeg/ffprobe/yt-dlp стоят таймауты; зависший процесс убивается.

## Доступ для других людей
- **По паролю** (`ACCESS_PASSWORD=...` в `.env`): человек пишет `/start` и отправляет пароль. Сообщение с паролем бот удаляет. После этого человек может смотреть статус, делать `/check` и получать алерты, а `/stop` отписывает от алертов. После 5 неверных попыток подряд вход блокируется на 15 минут.
- **Открытый** (`OPEN_ACCESS=true`, пароль пустой): ботом может пользоваться любой.
- **Админы** (`ALLOWED_USER_IDS`) всегда имеют полный доступ: `/add`, `/remove`, `/mute`, `/interval`, а также `/users` (кто получил доступ) и `/kick <id>` (отозвать доступ).
- Смена пароля не отзывает уже выданный доступ: для этого используйте `/kick`.

## Мониторинг vMix
1. В vMix: **Settings → Web Controller → Enable** (порт 8088).
2. В `.env`: `VMIX_HOST=<IP компьютера с vMix>` (если vMix на этом же ПК — `127.0.0.1`).
3. Перезапустите бота.

Раз в 5 секунд бот опрашивает `http://VMIX_HOST:8088/api` и присылает 🔴, если:
- vMix не отвечает 2 опроса подряд (программа закрыта, зависла или пропала сеть);
- выключился Stream (после того как он был включён);
- во время стрима включён Fade To Black (у зрителей чёрный экран);
- Master выключен (mute) или на мастер-выходе тишина дольше `VMIX_SILENCE_SEC` (15 с, порог `VMIX_SILENCE_DB=-60`).

Когда проблема исправлена, приходит 🟢. Если остановилась запись или в эфир поставили ролик на паузе, это 🟡: только в журнал.
Когда падает эфир, алерт подсказывает, где проблема: vMix не стримит → на источнике; vMix стримит → между vMix и платформой.

## Перенос на другой компьютер (например, сервер с vMix)
1. Скопируйте `dist\StreamMonitor.zip` на новый компьютер и распакуйте, например в `C:\`. В архиве лежат `.env` с токеном и база с эфирами: передавайте архив только по защищённому каналу.
2. На новом компьютере выполните:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Monitor-Python\deploy\setup_windows.ps1
   ```
   Скрипт ставит Python-зависимости и ffmpeg, прописывает `VMIX_HOST=127.0.0.1`, включает автозапуск и запускает бота.
3. На **старом** компьютере остановите бота, иначе два бота с одним токеном будут мешать друг другу:
   ```powershell
   Stop-ScheduledTask StreamMonitorBot; Unregister-ScheduledTask StreamMonitorBot -Confirm:$false
   ```

## Автозапуск в Windows
```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1
```
Скрипт создаёт задачу **StreamMonitorBot** в Планировщике заданий. Бот запускается без окна (`pythonw`) через 30 с после входа в Windows и перезапускается раз в минуту, если упал. Работает всегда только один экземпляр.
- Остановить: `Stop-ScheduledTask StreamMonitorBot`
- Запустить: `Start-ScheduledTask StreamMonitorBot`
- Удалить автозапуск: `Unregister-ScheduledTask StreamMonitorBot -Confirm:$false`

## Docker
```bash
docker build -t stream-monitor .
docker run -d --name stream-monitor --restart unless-stopped \
  --env-file .env -v $(pwd)/data:/app/data -v $(pwd)/logs:/app/logs stream-monitor
```
Если vMix находится в локальной сети, добавьте `--network host` (Linux).

## systemd
```bash
sudo useradd --system --home /opt/stream-monitor streammon
sudo cp -r . /opt/stream-monitor && cd /opt/stream-monitor
sudo -u streammon python3 -m venv .venv && sudo -u streammon .venv/bin/pip install -r requirements.txt
sudo mkdir -p data logs && sudo chown -R streammon: /opt/stream-monitor
sudo chmod 600 .env
sudo cp deploy/stream-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now stream-monitor
journalctl -u stream-monitor -f
```

## Структура
```
bot/        handlers (команды), middlewares (whitelist, rate limit), notifier, formatting
monitor/    engine (воркер на каждый эфир), state_machine, alerts (дедуп/корреляция/напоминания),
            reports (24ч), system (psutil + vMix)
checks/     validators (URL/SSRF), network (DNS/TCP/HTTP/TLS), hls, media (ffprobe/ffmpeg),
            resolver (yt-dlp), vmix, diagnostics (оркестратор)
storage/    db (SQLite: streams, events, metrics, incidents, settings)
```
Данные хранятся в `data/monitor.db`, логи пишутся в `logs/bot.log` (ротация 5×5 МБ).
