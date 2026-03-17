"""
Doc Orchestra — MVP Demo
========================
Запуск: python mvp.py
Затем открой в браузере:
  Родитель : http://localhost:8081/parent
  Врач     : http://localhost:8081/cockpit
"""
import asyncio
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

SERVER_START = int(time.time())  # уникальная версия — меняется при каждом запуске

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

# --------------------------------------------------------------------------
# Загрузка .env
# --------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEMO_MODE = not bool(ANTHROPIC_API_KEY)
COCKPIT_PASSWORD = os.environ.get("COCKPIT_PASSWORD", "doctor123")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DOCTOR_CHAT_ID = os.environ.get("DOCTOR_CHAT_ID", "")
DOCTOR_NAME = os.environ.get("DOCTOR_NAME", "врача")
WORK_START = int(os.environ.get("WORK_START", "9"))   # рабочий день с 9:00
WORK_END = int(os.environ.get("WORK_END", "20"))       # рабочий день до 20:00
REMINDER_TIMEOUT_MIN = int(os.environ.get("REMINDER_TIMEOUT_MIN", "30"))  # напоминание через 30 мин

if DEMO_MODE:
    print("\n⚠️  DEMO MODE — ANTHROPIC_API_KEY не найден.")
else:
    print(f"\n✅ REAL MODE — Claude API подключён.\n")

# --------------------------------------------------------------------------
# Хранилище в памяти
# --------------------------------------------------------------------------
sessions: dict = {}
appointments: list = []          # записи на приём [{id, session_id, label, date, time, note}]
cockpit_ws: list = []        # WebSocket-клиенты кокпита (устарело, оставлено для совместимости)
parent_ws: dict = {}
_cockpit_sessions: set = set()   # активные сессии кокпита (токены)
sse_queues: list = []            # SSE-очереди кокпита (основной канал событий)

# --------------------------------------------------------------------------
# ИИ
# --------------------------------------------------------------------------

DEMO_RESPONSES = {
    "greeting": "Здравствуйте! Я медицинский ассистент. Расскажите, пожалуйста, что беспокоит вашего ребёнка?",
    "complaint": "Понимаю, это беспокоит. Как давно появились симптомы? Есть ли температура?",
    "details": "Хорошо, записал. Уточните — какой вес ребёнка и были ли похожие эпизоды раньше?",
    "waiting": (
        "Спасибо. Я передал всю информацию доктору — он ответит в ближайшее время. "
        "Если состояние ухудшится или появится затруднённое дыхание — немедленно вызывайте скорую 103."
    ),
}

DEMO_SOAP = """📋 SOAP ЗАМЕТКА — Пациент (4 года, Ж)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

S — Субъективно:
  Жалобы: температура 38.5°C, сыпь на лице
  Длительность: с утра (~8 часов)
  Сопутствующие симптомы: вялость, снижение аппетита

O — Объективно:
  Вес: 14 кг
  Температура: 38.5°C
  Сыпь: гиперемия кожи лица, характер бледнеющий при надавливании

A — Предварительная оценка:
  Рабочий диагноз: ОРВИ с экзантемой (предположительно)
  Дифференциальный: краснуха, розеола, аллергическая реакция
  ⚠️ Красный флаг: не выявлен — сыпь бледнеет при надавливании

P — Рекомендуемые действия:
  Осмотр очный или видео-консультация
  Парацетамол 15 мг/кг при t > 38.5°C
  Контроль через 24 часа

💬 Черновик ответа родителю:
Здравствуйте! Доктор ознакомился с информацией. По описанию — картина ОРВИ с кожной реакцией, не требует срочной госпитализации. При температуре выше 38.5°C давайте парацетамол из расчёта 15 мг/кг. Обильное питьё. Если сыпь распространится или появится затруднённое дыхание — обратитесь немедленно. Свяжемся с вами для уточнения через 24 часа."""


def _demo_smart_response(session_id: str, user_message: str, phase: str) -> str:
    session = sessions.get(session_id, {})
    all_msgs = " ".join(
        m["text"].lower() for m in session.get("messages", []) if m["role"] == "parent"
    ) + " " + user_message.lower()

    told_temp = any(w in all_msgs for w in ["температур", "жар", "горит", "38", "39", "40", "37"])
    told_duration = any(w in all_msgs for w in ["минут", "час", "день", "дней", "давно", "сегодня", "вчера", "неделю"])
    told_weight = any(w in all_msgs for w in ["кг", "килограмм", "вес "])
    told_rash = any(w in all_msgs for w in ["сыпь", "пятн", "краснот", "высыпан", "точки"])
    told_age = any(w in all_msgs for w in ["лет", "месяц", "год", "годик"])

    frustrated = any(w in user_message.lower() for w in [
        "при чем", "зачем", "я же", "уже говор", "уже сказ", "почему вы", "не понимаю"
    ])
    emergency = any(w in all_msgs for w in [
        "не дышит", "без сознания", "судорог", "синеет", "задыхается", "потерял сознание"
    ])

    if emergency:
        return (
            "⚠️ Это требует НЕМЕДЛЕННОЙ помощи!\n\n"
            "Пожалуйста, прямо сейчас вызовите скорую — 103.\n\n"
            "Пока едет скорая:\n• Уложите ребёнка на бок\n"
            "• Не оставляйте одного\n• Расстегните одежду\n\n"
            "Врач уведомлён о срочном обращении."
        )

    if frustrated:
        return (
            "Понимаю ваше беспокойство, и прошу прощения. "
            "Скажите главное: есть ли у ребёнка затруднённое дыхание "
            "или сыпь не бледнеет при надавливании?"
        )

    if phase == "greeting":
        return DEMO_RESPONSES["greeting"]

    # Основной диалог (phase == "active") — задаём вопросы пока не соберём нужное
    # Когда всё есть — возвращаем [ГОТОВО] чтобы кокпит получил запрос
    if told_temp and told_duration and told_age:
        return (
            "Записал всё необходимое. "
            "Передаю информацию врачу — он свяжется с вами в ближайшее время. "
            "Если состояние ухудшится или появится затруднённое дыхание — вызывайте скорую 103. [ГОТОВО]"
        )
    if not told_temp:
        return "Есть ли у ребёнка температура прямо сейчас? Если да — какая?"
    if not told_duration:
        return "Как давно всё началось? Сколько часов или дней?"
    if not told_age:
        return "Сколько лет ребёнку?"
    return "Есть что-то ещё важное, о чём хотите сообщить врачу?"


async def get_ai_response(session_id: str, user_message: str, phase: str) -> str:
    if DEMO_MODE:
        await asyncio.sleep(1.2)
        return _demo_smart_response(session_id, user_message, phase)

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        session = sessions.get(session_id, {})

        messages = []
        for m in session.get("messages", [])[-12:]:
            role = "user" if m["role"] == "parent" else "assistant"
            messages.append({"role": role, "content": m["text"]})

        # Claude API требует непустой контент
        msg_content = user_message.strip() or "Поприветствуй родителя и предложи рассказать о проблеме ребёнка."
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": msg_content})
        else:
            messages[-1]["content"] = msg_content

        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=f"""Ты — личный ассистент {DOCTOR_NAME}. Собираешь информацию о ребёнке, пока врач занят, чтобы он сразу видел полную картину и мог быстро помочь.

ПРАВИЛА ДИАЛОГА:
1. Отвечай кратко и с заботой. Родитель переживает за ребёнка — это всегда тревожно.
2. Задавай СТРОГО ОДИН вопрос за раз.
3. Внимательно читай историю — не повторяй вопросы на которые уже ответили.
4. Если родитель сказал что чего-то НЕТ — не уточняй повторно.
5. Если родитель раздражён — коротко извинись и задай следующий вопрос.
6. Если спрашивают про запись/приём — отвечай: "{DOCTOR_NAME} свяжется с вами сразу после того как я передам информацию."
7. ЭКСТРЕННО (не дышит / судороги / потеря сознания) → только: "Немедленно вызовите скорую — 103!"
8. Никогда не ставь диагнозы и не назначай лечение.
9. НИКОГДА не говори "хорошо", "нормально", "это нормально", "спасибо за информацию" в ответ на симптомы или беспокойство — это звучит равнодушно. Вместо этого: "Понял", "Записал", "Не переживайте, я всё фиксирую и сразу передам врачу".

ЧТО СОБРАТЬ (в порядке):
1. Главная жалоба — родитель сам рассказывает
2. Температура — если не назвали
3. Как давно началось
4. Возраст ребёнка

КОГДА ЗАКАНЧИВАТЬ:
Как только у тебя есть: жалоба + температура + длительность + возраст — напиши прощальное сообщение И добавь в самом конце маркер [ГОТОВО].

Прощальное сообщение должно:
- Кратко подтвердить что собрал: "Записал: температура Х°С, [симптомы], [возраст], началось [когда]."
- Сообщить что передаёшь {DOCTOR_NAME}
- Если родитель спрашивал про приём — ответить на это
- Закончить словами "Спасибо за доверие."

Пример финального сообщения:
"Записал: температура 38,5°С, красные пятна на лице, 5 лет, началось сегодня. Передаю {DOCTOR_NAME} — он свяжется с вами в течение нескольких минут. По поводу приёма — уточнит сам. Спасибо за доверие. [ГОТОВО]"

ВАЖНО: маркер [ГОТОВО] — только в самом конце последнего сообщения. В остальных сообщениях его не используй.""",
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        return f"Извините, произошла техническая ошибка. Пожалуйста, обратитесь к врачу напрямую. ({e})"


async def generate_soap(session_id: str) -> str:
    session = sessions.get(session_id, {})
    msgs = session.get("messages", [])

    if DEMO_MODE:
        await asyncio.sleep(1.5)
        return DEMO_SOAP

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        history_text = "\n".join(
            f"{'Родитель' if m['role'] == 'parent' else 'Ассистент'}: {m['text']}"
            for m in msgs
        )
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system="Ты клинический ассистент педиатра. Составляй краткие структурированные SOAP заметки на русском языке.",
            messages=[{"role": "user", "content": f"""На основе диалога составь SOAP заметку для врача-педиатра.

ДИАЛОГ:
{history_text}

Формат:
📋 SOAP ЗАМЕТКА
━━━━━━━━━━━━━━━━━━━━━━

S — Субъективно:
  (жалобы, длительность, динамика)

O — Объективно:
  (симптомы: температура, сыпь, вес, возраст — всё что сообщил родитель)

A — Предварительная оценка:
  (возможные диагнозы для врача, красные флаги если есть)

P — Рекомендуемые действия:
  (какие анализы/осмотр, что уточнить)

💬 Черновик ответа родителю:
(тёплый текст БЕЗ диагноза, что делать пока ждёт)"""}],
        )
        return response.content[0].text
    except Exception as e:
        return f"Ошибка генерации SOAP: {e}"


def extract_patient_label(session: dict) -> str:
    """Извлекает имя/возраст пациента из переписки для отображения в кокпите."""
    all_text = " ".join(m["text"] for m in session.get("messages", []) if m["role"] == "parent")

    import re
    # Ищем возраст (поддержка "3,5 года", "1.5 лет")
    age_match = re.search(r'(\d+[,.]?\d*)\s*(лет|год|годик|месяц)', all_text)
    age = age_match.group(0) if age_match else None

    # Ищем имя (слово с большой буквы после "зовут", "имя", "ребёнка")
    name_match = re.search(r'(?:зовут|имя|ребёнка|дочь|сын)\s+([А-ЯЁ][а-яё]+)', all_text)
    name = name_match.group(1) if name_match else None

    if name and age:
        return f"{name}, {age}"
    if name:
        return name
    if age:
        return f"Ребёнок {age}"
    # Берём первые слова первого сообщения как превью
    first_msg = next((m["text"] for m in session.get("messages", []) if m["role"] == "parent"), "")
    return first_msg[:25] + "…" if len(first_msg) > 25 else first_msg or "Новый запрос"


# --------------------------------------------------------------------------
# Логика сессии
# --------------------------------------------------------------------------

def get_or_create_session(session_id: str) -> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "id": session_id,
            "messages": [],
            "soap": None,
            "status": "active",
            "phase": "greeting",
            "created_at": datetime.now().strftime("%H:%M"),
        }
    return sessions[session_id]


async def send_telegram_notification(label: str, preview: str, is_emergency: bool = False):
    """Отправить уведомление врачу в Telegram. Если токен не задан — молча пропускаем."""
    if not TELEGRAM_BOT_TOKEN or not DOCTOR_CHAT_ID:
        return
    import urllib.request
    import urllib.parse
    prefix = "🚨 ЭКСТРЕННО" if is_emergency else "🔔 Новый запрос"
    text = f"{prefix}\n👤 {label}\n\n{preview[:200]}\n\n👉 Откройте кокпит для ответа"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": DOCTOR_CHAT_ID, "text": text}).encode()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(url, payload, timeout=5))
    except Exception as e:
        print(f"⚠️  Telegram уведомление не отправлено: {e}")


async def notify_cockpit(event: dict):
    msg = json.dumps(event, ensure_ascii=False)
    # SSE (основной канал — работает в Safari)
    for q in list(sse_queues):
        try:
            await q.put(msg)
        except Exception:
            pass
    # WebSocket (оставлен для совместимости)
    dead = []
    for ws in cockpit_ws:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        cockpit_ws.remove(ws)


def is_working_hours() -> bool:
    """Проверить: сейчас рабочие часы врача?"""
    h = datetime.now().hour
    return WORK_START <= h < WORK_END


def get_offhours_message() -> str:
    """Сообщение для родителя в нерабочее время."""
    h = datetime.now().hour
    if h >= 20:
        return f"🌙 Сейчас поздний вечер, {DOCTOR_NAME} уже не на приёме. Ваше сообщение принято — он ответит завтра утром до 9:00. Если что-то срочное — вызовите скорую 103."
    else:
        return f"🌙 Сейчас ночное время, {DOCTOR_NAME} отдыхает. Ваше сообщение принято — он ответит утром до 9:00. Если что-то срочное — вызовите скорую 103."


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

from contextlib import asynccontextmanager


async def reminder_loop():
    """Фоновая задача: каждые 5 мин проверяем не одобренные запросы.
    Если ждут дольше REMINDER_TIMEOUT_MIN — отправляем Telegram-напоминание."""
    reminded: dict = {}  # session_id → время последнего напоминания
    while True:
        await asyncio.sleep(300)  # проверяем каждые 5 минут
        now_ts = datetime.now()
        for sid, s in list(sessions.items()):
            if s.get("status") != "waiting_doctor":
                continue
            created_str = s.get("created_at", "")
            if not created_str:
                continue
            try:
                # created_at хранится в формате "%H:%M" — восстанавливаем как сегодняшнюю дату
                created_time = datetime.strptime(created_str, "%H:%M").replace(
                    year=now_ts.year, month=now_ts.month, day=now_ts.day
                )
            except ValueError:
                continue
            wait_min = (now_ts - created_time).total_seconds() / 60
            last_reminded = reminded.get(sid, 0)
            # Напоминаем если: ждёт дольше порога И ещё не напоминали (или прошло ещё раз столько же)
            if wait_min >= REMINDER_TIMEOUT_MIN and (last_reminded == 0 or wait_min >= last_reminded + REMINDER_TIMEOUT_MIN):
                reminded[sid] = wait_min
                label = extract_patient_label(s)
                text = f"⏰ Запрос ожидает одобрения уже {int(wait_min)} мин.\n👤 {label}\n\nОткройте кокпит чтобы ответить."
                print(f"⏰ Напоминание врачу: {label}, ждёт {int(wait_min)} мин")
                asyncio.create_task(send_telegram_notification(label, text))


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(reminder_loop())
    yield


app = FastAPI(title="Doc Orchestra MVP", lifespan=lifespan)

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вход — Кокпит врача</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0;
       min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { background: #16213e; border: 1px solid #2a2a4a; border-radius: 16px;
        padding: 40px 36px; width: 100%; max-width: 360px; }
h1 { font-size: 22px; margin-bottom: 4px; }
.sub { color: #888; font-size: 14px; margin-bottom: 32px; }
label { display: block; font-size: 13px; color: #aaa; margin-bottom: 6px; }
input { width: 100%; padding: 12px 14px; background: #1a1a2e; border: 1px solid #3a3a5a;
        border-radius: 8px; color: #e0e0e0; font-size: 15px; outline: none;
        transition: border-color 0.15s; }
input:focus { border-color: #ff6b35; }
.field { margin-bottom: 20px; }
button { width: 100%; padding: 13px; background: #ff6b35; color: white; border: none;
         border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer;
         transition: opacity 0.15s; }
button:hover { opacity: 0.9; }
.error { color: #ff4444; font-size: 13px; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="card">
  <h1>🩺 Кокпит врача</h1>
  <p class="sub">Doc Orchestra</p>
  ERROR_PLACEHOLDER
  <form method="POST" action="/login">
    <div class="field">
      <label>Пароль</label>
      <input type="password" name="password" placeholder="Введите пароль" autofocus>
    </div>
    <button type="submit">Войти</button>
  </form>
</div>
</body>
</html>"""


def _check_session(request: Request) -> bool:
    token = request.cookies.get("cockpit_session")
    return token in _cockpit_sessions if token else False

# Ключевые слова экстренных состояний — проверяем ДО вызова API
EMERGENCY_WORDS = [
    "не дышит", "без сознания", "судорог", "синеет", "задыхается",
    "потерял сознание", "остановилось сердце", "посинел",
]


@app.get("/", response_class=HTMLResponse)
async def root():
    mode_text = "⚠️ DEMO MODE" if DEMO_MODE else "✅ Claude AI подключён"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doc Orchestra</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh;
       display: flex; align-items: center; justify-content: center; }}
.card {{ text-align: center; padding: 48px 40px; }}
h1 {{ font-size: 32px; margin-bottom: 8px; }}
.sub {{ color: #888; margin-bottom: 48px; font-size: 16px; }}
.links {{ display: flex; gap: 20px; justify-content: center; flex-wrap: wrap; }}
a {{ padding: 18px 36px; border-radius: 12px; text-decoration: none; font-size: 16px; font-weight: 600; transition: opacity 0.15s; }}
a:hover {{ opacity: 0.85; }}
.parent {{ background: #25D366; color: white; }}
.cockpit {{ background: #ff6b35; color: white; }}
.mode {{ margin-top: 32px; font-size: 13px; color: #666; }}
</style>
</head>
<body>
<div class="card">
  <h1>🎻 Doc Orchestra</h1>
  <p class="sub">AI-ассистент для педиатрической клиники</p>
  <div class="links">
    <a href="/parent" class="parent">📱 Интерфейс родителя</a>
    <a href="/cockpit" class="cockpit">🩺 Кокпит врача</a>
  </div>
  <p class="mode">{mode_text}</p>
</div>
</body></html>""")


# --------------------------------------------------------------------------
# РОДИТЕЛЬ — чат
# --------------------------------------------------------------------------

PARENT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Медицинский ассистент</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #e5ddd5; height: 100dvh; display: flex; flex-direction: column; }

.header { background: #075e54; color: white; padding: 14px 18px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.avatar { width: 42px; height: 42px; background: #25D366; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }
.header-info h3 { font-size: 15px; font-weight: 600; }
.header-info p { font-size: 12px; opacity: 0.75; margin-top: 2px; }
#status-indicator { font-size: 12px; opacity: 0.75; }

.messages { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; }
.msg { max-width: 78%; padding: 8px 12px 6px; border-radius: 8px; font-size: 14px; line-height: 1.55; word-break: break-word; }
.msg.bot { background: white; align-self: flex-start; border-radius: 0 8px 8px 8px; box-shadow: 0 1px 1px rgba(0,0,0,0.08); }
.msg.user { background: #dcf8c6; align-self: flex-end; border-radius: 8px 0 8px 8px; box-shadow: 0 1px 1px rgba(0,0,0,0.08); }
.msg.system { background: rgba(255,255,255,0.85); align-self: center; font-size: 12px; color: #555; border-radius: 8px; padding: 7px 14px; max-width: 88%; text-align: center; border: 1px solid #ddd; }
.msg.emergency { background: #ffe4e4; border: 1px solid #ff4444; align-self: center; max-width: 92%; text-align: center; }
.msg .time { font-size: 10px; color: #aaa; text-align: right; margin-top: 3px; }

.typing { background: white; padding: 12px 16px; border-radius: 0 8px 8px 8px; display: inline-flex; gap: 4px; align-self: flex-start; box-shadow: 0 1px 1px rgba(0,0,0,0.08); }
.typing span { width: 7px; height: 7px; background: #bbb; border-radius: 50%; animation: bounce 1.2s infinite; }
.typing span:nth-child(2) { animation-delay: 0.2s; }
.typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-7px)} }

.waiting-banner { background: #fff8e1; border-top: 1px solid #ffe082; padding: 12px 18px; text-align: center; font-size: 13px; color: #795548; flex-shrink: 0; display: none; }
.waiting-banner.show { display: block; }

.input-area { background: #f0f0f0; padding: 10px 14px; display: flex; gap: 8px; align-items: flex-end; flex-shrink: 0; }
textarea { flex: 1; padding: 10px 14px; border: none; border-radius: 22px; resize: none; font-size: 14px; outline: none; max-height: 120px; line-height: 1.4; font-family: inherit; background: white; }
textarea:disabled { background: #f5f5f5; color: #aaa; }
button#sendBtn { width: 44px; height: 44px; background: #25D366; border: none; border-radius: 50%; cursor: pointer; color: white; font-size: 20px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background 0.15s; }
button#sendBtn:disabled { background: #ccc; cursor: default; }
</style>
</head>
<body>
<div class="header">
  <div class="avatar">🩺</div>
  <div class="header-info">
    <h3>Медицинский ассистент</h3>
    <p id="status-indicator">На связи</p>
  </div>
</div>

<div class="messages" id="messages"></div>

<div class="waiting-banner" id="waiting-banner">
  <span id="waiting-text">⏳ Информация передана доктору — ожидайте ответа.</span>
  &nbsp;·&nbsp; При ухудшении — 📞 103
  &nbsp;·&nbsp;
  <button onclick="newChat()" style="background:none;border:none;color:#795548;text-decoration:underline;cursor:pointer;font-size:13px;padding:0;">Новый чат</button>
</div>

<div class="input-area">
  <textarea id="input" placeholder="Напишите сообщение..." rows="1"
    oninput="autoResize(this)" onkeydown="handleKey(event)"></textarea>
  <button id="sendBtn" onclick="sendMessage()">➤</button>
</div>

<script>
// session_id хранится в localStorage — не сбрасывается при перезагрузке страницы
const _PERSIST_KEY = 'doc_orch_sid';
const SESSION_ID = localStorage.getItem(_PERSIST_KEY) || (() => {
  const id = Math.random().toString(36).substr(2, 9);
  localStorage.setItem(_PERSIST_KEY, id);
  return id;
})();
const STORAGE_KEY = 'doc_orch_' + SESSION_ID;
let ws;
let locked = false; // заблокировано после передачи врачу

// Восстановить историю из localStorage
function restoreHistory() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return false;
    const data = JSON.parse(saved);
    if (data.messages) {
      data.messages.forEach(m => addMessageRaw(m.text, m.role, m.time, false));
    }
    if (data.locked) {
      setLocked(true);
    }
    return true;
  } catch(e) { return false; }
}

function saveToStorage(text, role) {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    const data = saved ? JSON.parse(saved) : { messages: [], locked: false };
    data.messages.push({ text, role, time: now() });
    data.locked = locked;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  } catch(e) {}
}

function getWaitingText() {
  const h = new Date().getHours();
  if (h >= 9 && h < 20) {
    return '⏳ Передано врачу — ответит в течение часа.';
  } else if (h >= 20 && h < 23) {
    return '🌙 Поздний вечер — врач ответит завтра до 9:00.';
  } else {
    return '🌙 Ночное время — врач ответит утром до 9:00.';
  }
}

function setLocked(val) {
  locked = val;
  const inp = document.getElementById('input');
  const btn = document.getElementById('sendBtn');
  const banner = document.getElementById('waiting-banner');
  inp.disabled = val;
  btn.disabled = val;
  if (val) {
    banner.classList.add('show');
    const wtEl = document.getElementById('waiting-text');
    if (wtEl) wtEl.textContent = getWaitingText();
    document.getElementById('status-indicator').textContent = '⏳ Ожидает ответа врача';
    inp.placeholder = 'Ожидайте ответа врача...';
  }
}

function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws/parent/' + SESSION_ID);
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'message') {
      addMessageRaw(data.text, 'bot', null, true);
    } else if (data.type === 'typing') {
      showTyping(data.show);
    } else if (data.type === 'status_update') {
      document.getElementById('status-indicator').textContent = data.text || '';
      if (data.system_msg) addMessageRaw(data.system_msg, 'system', null, true);
      if (data.locked) setLocked(true);
    } else if (data.type === 'doctor_reply') {
      setLocked(false);
      document.getElementById('status-indicator').textContent = '✅ Врач ответил';
      document.getElementById('waiting-banner').classList.remove('show');
      addMessageRaw(data.text, 'bot', null, true);
    }
  };
  ws.onclose = () => {
    document.getElementById('status-indicator').textContent = 'Переподключение...';
    setTimeout(connect, 2500);
  };
  ws.onopen = () => {
    document.getElementById('status-indicator').textContent = 'На связи';
  };
}

function now() {
  return new Date().toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
}

function addMessageRaw(text, role, time, save) {
  removeTyping();
  const div = document.getElementById('messages');
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  const t = time || now();
  const isSystem = role === 'system';
  el.innerHTML = text.split(String.fromCharCode(10)).join('<br>') + (isSystem ? '' : '<div class="time">' + t + '</div>');
  div.appendChild(el);
  div.scrollTop = div.scrollHeight;
  if (save) saveToStorage(text, role);
}

function showTyping(show) {
  removeTyping();
  if (show) {
    const div = document.getElementById('messages');
    const el = document.createElement('div');
    el.id = 'typing';
    el.className = 'typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    div.appendChild(el);
    div.scrollTop = div.scrollHeight;
  }
}
function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || locked) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    // WebSocket ещё не готов — ждём и повторяем через 800ms
    document.getElementById('status-indicator').textContent = 'Подключение...';
    setTimeout(() => sendMessage(), 800);
    return;
  }
  addMessageRaw(text, 'user', null, true);
  ws.send(JSON.stringify({type: 'message', text: text}));
  document.getElementById('status-indicator').textContent = '✓ Сообщение получено';
  input.value = '';
  input.style.height = 'auto';
}

function newChat() {
  localStorage.removeItem(STORAGE_KEY);
  localStorage.removeItem(_PERSIST_KEY);
  location.reload();
}

// При загрузке страницы
const hadHistory = restoreHistory();
connect();
// Если история была — не ждём нового приветствия
if (hadHistory) {
  // восстановлено, не посылаем запрос на приветствие
}
</script>
</body></html>"""


@app.get("/parent", response_class=HTMLResponse)
async def parent_page():
    # session_id генерируется и хранится на клиенте (localStorage) — не сбрасывается при перезагрузке
    return HTMLResponse(PARENT_HTML)


@app.websocket("/ws/parent/{session_id}")
async def parent_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    parent_ws[session_id] = websocket
    session = get_or_create_session(session_id)

    # Приветствие запускаем фоновой задачей — не блокируем приём сообщений
    async def send_greeting():
        if not session["messages"]:
            await websocket.send_text(json.dumps({"type": "typing", "show": True}))
            greeting = await get_ai_response(session_id, "", "greeting")
            await websocket.send_text(json.dumps({"type": "typing", "show": False}))
            await websocket.send_text(json.dumps({"type": "message", "text": greeting}))
            session["messages"].append({"role": "bot", "text": greeting, "time": datetime.now().isoformat()})
            # Автоответ о нерабочем времени — только при первом подключении
            if not is_working_hours():
                await asyncio.sleep(0.8)
                offhours_msg = get_offhours_message()
                await websocket.send_text(json.dumps({"type": "message", "text": offhours_msg}))
                session["messages"].append({"role": "bot", "text": offhours_msg, "time": datetime.now().isoformat()})

    asyncio.create_task(send_greeting())

    # Если сессия уже в ожидании врача — восстановить статус
    if session.get("status") == "waiting_doctor":
        await websocket.send_text(json.dumps({
            "type": "status_update",
            "text": "⏳ Ожидает ответа врача",
            "locked": True
        }))

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            if data.get("type") != "message":
                continue

            # Блокируем входящие сообщения если уже у врача
            if session.get("status") == "waiting_doctor":
                await websocket.send_text(json.dumps({
                    "type": "message",
                    "text": "ℹ️ Ваш запрос уже передан доктору. Ожидайте ответа."
                }))
                continue

            user_text = data["text"]
            session["messages"].append({"role": "parent", "text": user_text, "time": datetime.now().isoformat()})

            # Экстренный путь — отвечаем МГНОВЕННО, без вызова Claude API
            # Работает даже если API недоступен
            if any(kw in user_text.lower() for kw in EMERGENCY_WORDS):
                emergency_reply = (
                    "⚠️ НЕМЕДЛЕННО вызовите скорую — 103!\n\n"
                    "Пока едет скорая:\n"
                    "• Уложите ребёнка на бок\n"
                    "• Не оставляйте одного\n"
                    "• Расстегните одежду\n\n"
                    "Врач уже уведомлён."
                )
                await websocket.send_text(json.dumps({"type": "message", "text": emergency_reply}))
                session["messages"].append({"role": "bot", "text": emergency_reply})
                session["status"] = "waiting_doctor"
                emergency_soap = f"🚨 ЭКСТРЕННОЕ ОБРАЩЕНИЕ\n\nСообщение родителя: {user_text}\n\nТребуется НЕМЕДЛЕННАЯ связь с семьёй."
                session["soap"] = emergency_soap
                await websocket.send_text(json.dumps({
                    "type": "status_update",
                    "text": "🚨 Экстренный случай — врач уведомлён",
                    "locked": True,
                }))
                emergency_label = "🚨 ЭКСТРЕННО — " + extract_patient_label(session)
                await notify_cockpit({
                    "type": "new_case",
                    "session_id": session_id,
                    "label": emergency_label,
                    "preview": user_text[:60],
                    "soap": emergency_soap,
                    "time": datetime.now().strftime("%H:%M"),
                    "messages": [{"role": m["role"], "text": m["text"]} for m in session["messages"]],
                })
                asyncio.create_task(send_telegram_notification(
                    emergency_label, user_text[:200], is_emergency=True
                ))
                continue  # пропускаем обычный поток

            await websocket.send_text(json.dumps({"type": "typing", "show": True}))

            raw_reply = await get_ai_response(session_id, user_text, "active")
            is_ready = "[ГОТОВО]" in raw_reply
            bot_reply = raw_reply.replace("[ГОТОВО]", "").strip()

            await websocket.send_text(json.dumps({"type": "typing", "show": False}))
            await websocket.send_text(json.dumps({"type": "message", "text": bot_reply}))
            session["messages"].append({"role": "bot", "text": bot_reply})

            if is_ready:
                # Claude решил что информации достаточно — передаём врачу
                await websocket.send_text(json.dumps({
                    "type": "status_update",
                    "text": "⏳ Ожидает ответа врача",
                    "locked": True
                }))

                session["status"] = "waiting_doctor"
                soap = await generate_soap(session_id)
                session["soap"] = soap
                label = extract_patient_label(session)

                await notify_cockpit({
                    "type": "new_case",
                    "session_id": session_id,
                    "label": label,
                    "preview": bot_reply[:60],
                    "soap": soap,
                    "time": datetime.now().strftime("%H:%M"),
                    "messages": [
                        {"role": m["role"], "text": m["text"]}
                        for m in session["messages"]
                    ],
                })
                asyncio.create_task(send_telegram_notification(
                    label, bot_reply[:200]
                ))

    except WebSocketDisconnect:
        parent_ws.pop(session_id, None)


# --------------------------------------------------------------------------
# ВРАЧ — кокпит
# --------------------------------------------------------------------------

COCKPIT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Doc Orchestra — Кокпит врача</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100dvh; }

.header { background: #0f0f23; border-bottom: 1px solid #2a2a4a; padding: 14px 24px;
          display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 18px; font-weight: 700; color: white; }
.header-right { display: flex; align-items: center; gap: 12px; }
.badge { background: #ff6b35; color: white; border-radius: 20px; padding: 2px 10px; font-size: 12px; font-weight: 700; }
.mode-badge { padding: 4px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; }
.mode-demo { background: #3a2a00; color: #ff6b35; border: 1px solid #ff6b35; }
.mode-real { background: #002a15; color: #22c55e; border: 1px solid #22c55e; }
.hint { font-size: 11px; color: #555; }

.layout { display: flex; height: calc(100dvh - 57px); }

/* Sidebar */
.sidebar { width: 280px; background: #0f0f23; border-right: 1px solid #2a2a4a; display: flex; flex-direction: column; flex-shrink: 0; }
.sidebar-header { padding: 14px 16px; border-bottom: 1px solid #2a2a4a; font-size: 11px; font-weight: 700; color: #666; text-transform: uppercase; letter-spacing: 1px; }
.case-list { flex: 1; overflow-y: auto; }
.empty-sidebar { padding: 32px 16px; text-align: center; color: #444; font-size: 13px; line-height: 1.6; }
.empty-sidebar a { color: #ff6b35; text-decoration: none; }

.case-item { padding: 13px 16px; border-bottom: 1px solid #1a1a35; cursor: pointer; transition: background 0.1s; }
.case-item:hover { background: #1e1e3a; }
.case-item.active { background: #1e1e3a; border-left: 3px solid #ff6b35; }
.case-item .title { font-size: 14px; font-weight: 600; color: #e0e0e0; display: flex; align-items: center; gap: 7px; }
.case-item .preview { font-size: 12px; color: #666; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.case-item .meta { font-size: 11px; color: #555; margin-top: 4px; display: flex; justify-content: space-between; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot-waiting { background: #ff6b35; animation: pulse 1.5s infinite; }
.dot-approved { background: #22c55e; }
.dot-rejected { background: #555; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }

/* Main */
.main { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 18px; }
.empty-main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #444; gap: 10px; }
.empty-main .icon { font-size: 40px; }

/* Cards */
.card { background: #0f0f23; border: 1px solid #2a2a4a; border-radius: 12px; padding: 20px; }
.card-title { font-size: 13px; font-weight: 700; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }

/* Tabs: SOAP / Диалог / Анализы */
.tabs { display: flex; gap: 0; border-bottom: 1px solid #2a2a4a; margin-bottom: 16px; }
.tab { padding: 8px 18px; font-size: 13px; font-weight: 600; color: #555; cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.15s; }
.tab.active { color: #ff6b35; border-bottom-color: #ff6b35; }
.tab:hover { color: #aaa; }

.soap-content { font-family: 'Menlo', 'Monaco', monospace; font-size: 12.5px; line-height: 1.9; color: #c0c0d0; }
.soap-h1 { font-size: 14px; font-weight: 700; color: #ff6b35; margin: 12px 0 4px; }
.soap-h2 { font-size: 12.5px; font-weight: 700; color: #e0e0ff; margin: 10px 0 2px; }
.dialog-content { display: flex; flex-direction: column; gap: 8px; }

/* Анализы */
.analyze-panel { display: flex; flex-direction: column; gap: 12px; }
.analyze-input { background: #0a0a1a; border: 1px solid #2a2a4a; border-radius: 8px; color: #c0c0d0; font-family: 'Menlo','Monaco',monospace; font-size: 12.5px; line-height: 1.7; padding: 10px 12px; resize: vertical; min-height: 120px; width: 100%; box-sizing: border-box; }
.analyze-input:focus { outline: none; border-color: #ff6b35; }
.analyze-input::placeholder { color: #444; }
.analyze-result { background: #0a0a1a; border: 1px solid #2a2a4a; border-radius: 8px; padding: 14px; min-height: 80px; font-size: 13px; line-height: 1.7; color: #c0c0d0; white-space: pre-wrap; }
.analyze-result .ar-section { margin-bottom: 12px; }
.analyze-result .ar-title { font-size: 13px; font-weight: 700; color: #ff6b35; margin-bottom: 4px; }
.analyze-result .ar-normal { color: #6dbf7e; }
.analyze-result .ar-warn { color: #f0b429; }
.analyze-result .ar-crit { color: #ef4444; font-weight: 600; }
.analyze-result .ar-placeholder { color: #444; font-size: 13px; }
.d-msg { padding: 8px 12px; border-radius: 8px; font-size: 13px; line-height: 1.5; max-width: 85%; }
.d-msg.parent { background: #1e3a5f; color: #b0cce0; align-self: flex-end; }
.d-msg.bot { background: #1e1e35; color: #c0c0d0; align-self: flex-start; }
.d-msg .role { font-size: 10px; font-weight: 700; opacity: 0.6; margin-bottom: 3px; text-transform: uppercase; }

/* Actions */
.draft-label { font-size: 12px; font-weight: 700; color: #666; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
textarea.draft-edit { width: 100%; min-height: 90px; padding: 12px 14px; background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 8px; color: #e0e0e0; font-size: 13px; line-height: 1.55; resize: vertical; font-family: inherit; outline: none; transition: border-color 0.15s; }
textarea.draft-edit:focus { border-color: #ff6b35; }
.btn-row { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.btn { padding: 10px 22px; border: none; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 700; transition: opacity 0.15s; }
.btn:hover { opacity: 0.85; }
.btn:disabled { opacity: 0.4; cursor: default; }
.btn-approve { background: #22c55e; color: white; }
.btn-edit { background: #2a2a4a; color: #ccc; }
.btn-reject { background: #2a2a4a; color: #ef4444; border: 1px solid #ef4444; }
.btn-archive { background: #2a2a4a; color: #888; }

/* Reject form (inline) */
.reject-form { display: none; margin-top: 10px; }
.reject-form.show { display: block; }
.reject-form textarea { width: 100%; padding: 10px 12px; background: #1a1a2e; border: 1px solid #ef4444; border-radius: 8px; color: #e0e0e0; font-size: 13px; resize: none; font-family: inherit; outline: none; }
.reject-form .reject-actions { display: flex; gap: 8px; margin-top: 8px; }
.btn-reject-confirm { background: #ef4444; color: white; }

/* Шаблоны быстрых ответов */
.quick-replies { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.btn-quick { padding: 5px 12px; background: #1a1a3a; border: 1px solid #3a3a5a; border-radius: 20px;
             color: #aaa; font-size: 12px; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
.btn-quick:hover { border-color: #ff6b35; color: #ff6b35; background: #2a1a0a; }

/* Toast */
.toast { position: fixed; bottom: 24px; right: 24px; background: #1e1e3a; color: white;
         border: 1px solid #2a2a4a; padding: 12px 20px; border-radius: 10px; font-size: 13px;
         opacity: 0; transition: opacity 0.25s; pointer-events: none; z-index: 100; max-width: 320px; }
.toast.show { opacity: 1; }

/* Keyboard hint */
.kbd { display: inline-block; background: #2a2a4a; border-radius: 4px; padding: 1px 6px; font-size: 11px; color: #888; font-family: monospace; }

/* Кнопка расписания в шапке */
.btn-schedule { background: #2a2a4a; border: 1px solid #3a3a5a; color: #ccc; padding: 6px 14px;
                border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600; transition: all 0.15s; }
.btn-schedule:hover { border-color: #ff6b35; color: #ff6b35; }

/* Модал записи */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 200;
                 display: flex; align-items: center; justify-content: center; }
.modal-overlay.hidden { display: none; }
.modal { background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 14px;
         padding: 28px; width: 360px; max-width: 95vw; }
.modal h3 { font-size: 16px; font-weight: 700; color: white; margin-bottom: 18px; }
.modal label { display: block; font-size: 12px; color: #888; margin-bottom: 4px; margin-top: 12px; }
.modal input { width: 100%; background: #0f0f23; border: 1px solid #2a2a4a; border-radius: 8px;
               color: #e0e0e0; padding: 9px 12px; font-size: 14px; }
.modal input:focus { outline: none; border-color: #ff6b35; }
.modal-btns { display: flex; gap: 10px; margin-top: 20px; }
.modal-btns button { flex: 1; padding: 10px; border-radius: 8px; border: none;
                     cursor: pointer; font-size: 13px; font-weight: 700; }
.btn-modal-ok { background: #22c55e; color: white; }
.btn-modal-cancel { background: #2a2a4a; color: #888; }

/* Панель-календарь (слайд справа) */
.schedule-panel { position: fixed; top: 0; right: -520px; width: 500px; height: 100vh;
                  background: #0f0f23; border-left: 1px solid #2a2a4a; z-index: 150;
                  transition: right 0.3s ease; display: flex; flex-direction: column; }
.schedule-panel.open { right: 0; }
.schedule-header { padding: 14px 16px; border-bottom: 1px solid #1a1a35;
                   display: flex; align-items: center; gap: 8px; }
.schedule-header h2 { font-size: 14px; font-weight: 700; color: white; flex: 1; }
.schedule-close { background: none; border: none; color: #888; cursor: pointer; font-size: 20px; padding: 0; }
/* Навигация по неделям внутри панели */
.cal-nav { display: flex; align-items: center; gap: 6px; padding: 10px 14px;
           border-bottom: 1px solid #0d0d1f; background: #0a0a1a; }
.cal-nav-title { flex: 1; text-align: center; font-size: 12px; font-weight: 700; color: #ccc; }
.cal-btn { background: #1a1a2e; border: 1px solid #2a2a4a; color: #888; padding: 4px 10px;
           border-radius: 5px; cursor: pointer; font-size: 11px; font-weight: 600; transition: all 0.15s; }
.cal-btn:hover { border-color: #ff6b35; color: #ff6b35; }
/* Таблица-сетка внутри панели */
.schedule-body { flex: 1; overflow-y: auto; overflow-x: auto; }
.cal-table { width: 100%; border-collapse: collapse; min-width: 460px; }
.cal-table thead th { padding: 8px 3px 6px; text-align: center; font-size: 11px; font-weight: 700;
                      border-bottom: 2px solid #1a1a35; position: sticky; top: 0;
                      background: #0f0f23; z-index: 2; }
.cal-table th.today-col { background: rgba(255,107,53,0.08); }
.cal-day-name { display: block; font-size: 9px; color: #555; font-weight: 400; margin-bottom: 3px; }
.cal-day-num { font-size: 15px; color: #e0e0e0; display: inline-block; width: 24px;
               height: 24px; line-height: 24px; text-align: center; border-radius: 50%; }
.today-num { background: #ff6b35 !important; color: white !important; }
.cal-table tbody td { border-bottom: 1px solid #0d0d1f; border-right: 1px solid #0d0d1f;
                      vertical-align: top; padding: 2px 2px; min-height: 42px; }
.cal-table tbody td.today-col { background: rgba(255,107,53,0.04); }
.cal-time-cell { text-align: right; padding: 2px 6px 2px 3px !important; font-size: 10px;
                 color: #2a2a4a; font-weight: 700; white-space: nowrap; width: 38px;
                 vertical-align: middle; background: #08080f;
                 border-right: 2px solid #1a1a35 !important; }
.cal-appt { background: #1b3a5c; border-left: 3px solid #4a9eff; border-radius: 4px;
            padding: 3px 5px; margin: 2px 1px; overflow: hidden; }
.cal-appt-del { float: right; background: none; border: none; color: #2a4060;
                cursor: pointer; font-size: 10px; padding: 0; line-height: 1; }
.cal-appt-del:hover { color: #ef4444; }
.cal-appt-time { font-size: 9px; color: #64b5f6; font-weight: 700; }
.cal-appt-name { font-size: 10px; color: #c8e0ff; line-height: 1.3; margin-top: 1px; }
.cal-appt-note { font-size: 9px; color: #3a6a8a; margin-top: 1px; }
.sched-empty { text-align: center; color: #444; font-size: 13px; padding: 40px 0; }

@media (max-width: 640px) {
  .layout { flex-direction: column; }
  .sidebar { width: 100%; height: 140px; }
  .case-list { display: flex; flex-direction: row; overflow-x: auto; overflow-y: hidden; }
  .case-item { min-width: 200px; border-bottom: none; border-right: 1px solid #1a1a35; }
  .hint { display: none; }
}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:14px">
    <h1>🎻 Doc Orchestra</h1>
    <span class="badge" id="count-badge" style="display:none">0</span>
  </div>
  <div class="header-right">
    <span id="ws-status" style="font-size:12px;color:#4caf50;">🟢 Активен</span>
    <span style="font-size:10px;color:#333;" title="Версия сервера">v·SERVER_VERSION_PLACEHOLDER</span>
    <button class="btn-schedule" onclick="toggleSchedule()">📅 Расписание</button>
    <span class="hint"><span class="kbd">A</span> одобрить &nbsp;<span class="kbd">Esc</span> закрыть</span>
    <span class="mode-badge MODE_CLASS" id="mode-badge">MODE_PLACEHOLDER</span>
  </div>
</div>

<div class="layout">
  <div class="sidebar">
    <div class="sidebar-header" style="display:flex;align-items:center;justify-content:space-between;flex-direction:column;align-items:flex-start;gap:4px;">
      <div style="display:flex;align-items:center;justify-content:space-between;width:100%;">
        Очередь
        <button onclick="reloadSessions()" style="background:none;border:none;color:#ff6b35;cursor:pointer;font-size:13px;padding:0;" title="Обновить список">↺ обновить</button>
      </div>
      <div id="poll-status" style="font-size:10px;color:#444;font-weight:400;text-transform:none;letter-spacing:0;">Загрузка...</div>
    </div>
    <div class="case-list" id="case-list">
      <div class="empty-sidebar">
        Нет запросов<br><br>
        <a href="/parent" target="_blank">↗ Открыть чат родителя</a>
      </div>
    </div>
  </div>

  <div class="main" id="main-content">
    <div class="empty-main">
      <div class="icon">📋</div>
      <div style="font-size:15px;font-weight:600;color:#666">Очередь пуста</div>
      <div style="font-size:13px;color:#444">Новые запросы появятся здесь автоматически</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<!-- Модал: запись на приём -->
<div class="modal-overlay hidden" id="appt-modal">
  <div class="modal">
    <h3>📅 Запись на приём</h3>
    <div id="appt-patient-line" style="font-size:13px;color:#888;margin-bottom:4px;"></div>
    <label>Дата</label>
    <input type="date" id="appt-date">
    <label>Время</label>
    <input type="time" id="appt-time" value="10:00">
    <label>Примечание (необязательно)</label>
    <input type="text" id="appt-note" placeholder="Повторный осмотр, прививка...">
    <div class="modal-btns">
      <button class="btn-modal-ok" onclick="confirmAppointment()">✅ Записать</button>
      <button class="btn-modal-cancel" onclick="closeApptModal()">Отмена</button>
    </div>
  </div>
</div>

<!-- Панель расписания (слайд справа) -->
<div class="schedule-panel" id="schedule-panel">
  <div class="schedule-header">
    <h2>📅 Расписание приёмов</h2>
    <button class="schedule-close" onclick="toggleSchedule()">✕</button>
  </div>
  <div class="cal-nav">
    <button class="cal-btn" onclick="calPrevWeek()">‹</button>
    <span class="cal-nav-title" id="cal-nav-title"></span>
    <button class="cal-btn" onclick="calNextWeek()">›</button>
    <button class="cal-btn" onclick="calGoToday()">Сегодня</button>
  </div>
  <div class="schedule-body" id="schedule-body"></div>
</div>

<script>
let cases = {};
let activeSession = null;
let activeTab = 'soap';

// SSE — основной канал, мгновенно получает все текущие кейсы при подключении
let sseSource = null;

function connectSSE() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  const statusEl = document.getElementById('ws-status');
  const pollEl = document.getElementById('poll-status');
  sseSource = new EventSource('/api/events');
  sseSource.onopen = () => {
    if (statusEl) { statusEl.textContent = '🟢 Активен'; statusEl.style.color = '#4caf50'; }
    if (pollEl) pollEl.textContent = 'SSE подключён';
    console.log('[sse] connected');
  };
  sseSource.onmessage = (e) => {
    console.log('[sse] event:', e.data.slice(0, 80));
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'init') {
        // Все текущие кейсы при первом подключении
        (data.cases || []).forEach(c => {
          cases[c.session_id] = c;
        });
        renderSidebar();
        const first = (data.cases || [])[0];
        if (first && !activeSession) {
          try { selectCase(first.session_id); } catch(e) { console.error(e); }
        }
        const t = new Date().toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
        const total = (data.cases || []).filter(c => c.status !== 'archived').length;
        if (pollEl) pollEl.textContent = t + (total > 0 ? ' · ' + total + ' запр.' : ' · пусто');
      } else if (data.type === 'new_case') {
        const isNew = !cases[data.session_id];
        cases[data.session_id] = data;
        renderSidebar();
        if (isNew && !activeSession) {
          try { selectCase(data.session_id); } catch(e) { console.error(e); }
          showToast('🔔 Новый запрос: ' + (data.label || data.session_id));
        }
      } else if (data.type === 'case_update') {
        if (cases[data.session_id]) cases[data.session_id].status = data.status;
        renderSidebar();
      }
    } catch(err) { console.error('[sse] parse error:', err); }
  };
  sseSource.onerror = () => {
    if (statusEl) { statusEl.textContent = '🟡 Переподключение'; statusEl.style.color = '#ff9800'; }
    console.warn('[sse] error, will reconnect');
  };
}

// REST polling — запасной канал, каждые 5 секунд
function startPolling() {
  reloadSessions();
  setInterval(reloadSessions, 5000);
}

function connect() {
  // Проверяем что страница актуальна — защита от браузерного кеша
  const urlV = new URLSearchParams(window.location.search).get('v');
  const pageV = 'SERVER_VERSION_PLACEHOLDER';
  if (urlV && urlV !== pageV) {
    // Страница устарела — перезагружаем
    window.location.replace('/cockpit?v=' + pageV);
    return;
  }
  connectSSE();
  startPolling(); // polling как страховка
}

function handleNewCase(data, notify = true) {
  cases[data.session_id] = data;
  renderSidebar();
  if (!activeSession) selectCase(data.session_id);
  if (notify) {
    showToast('🔔 Новый запрос: ' + (data.label || data.session_id));
    // Попытка звукового уведомления
    try { new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAA...').play(); } catch(e) {}
  }
}

function renderSidebar() {
  const list = document.getElementById('case-list');
  const count = Object.values(cases).filter(c => c.status !== 'archived').length;
  const badge = document.getElementById('count-badge');
  const pending = Object.values(cases).filter(c => c.status === 'waiting_doctor' || !c.status).length;
  badge.textContent = pending;
  badge.style.display = pending > 0 ? 'inline' : 'none';

  const visible = Object.values(cases).filter(c => c.status !== 'archived');
  if (visible.length === 0) {
    list.innerHTML = '<div class="empty-sidebar">Нет запросов<br><br><a href="/parent" target="_blank">↗ Открыть чат родителя</a></div>';
    return;
  }

  list.innerHTML = visible.map(c => {
    const dotClass = c.status === 'approved' ? 'dot-approved' : c.status === 'rejected' ? 'dot-rejected' : 'dot-waiting';
    const statusText = c.status === 'approved' ? '✅ Отправлено' : c.status === 'rejected' ? '✗ Отклонено' : '⏳ Ожидает';
    return `<div class="case-item ${c.session_id === activeSession ? 'active' : ''}" onclick="selectCase('${c.session_id}')">
      <div class="title">
        <span class="status-dot ${dotClass}"></span>
        ${escapeHtml(c.label || c.session_id)}
      </div>
      <div class="preview">${escapeHtml(c.preview || '')}</div>
      <div class="meta"><span>${c.time}</span><span>${statusText}</span></div>
    </div>`;
  }).join('');
}

function selectCase(sessionId) {
  activeSession = sessionId;
  activeTab = 'soap';
  renderSidebar();
  const c = cases[sessionId];
  if (!c) return;

  const draft = extractDraft(c.soap || '');
  const isApproved = c.status === 'approved';
  const isRejected = c.status === 'rejected';
  const statusBadge = isApproved
    ? '<span style="color:#4caf50;font-size:12px;font-weight:600;">✅ Отправлен</span>'
    : isRejected
    ? '<span style="color:#ef4444;font-size:12px;font-weight:600;">✗ Отклонён</span>'
    : '';

  document.getElementById('main-content').innerHTML = `
    <div class="card">
      <div class="tabs">
        <div class="tab active" id="tab-soap" onclick="switchTab('soap')">📋 SOAP</div>
        <div class="tab" id="tab-dialog" onclick="switchTab('dialog')">💬 Диалог</div>
        <div class="tab" id="tab-analyze" onclick="switchTab('analyze')">🔬 Анализы</div>
      </div>
      <div id="panel-soap" class="soap-content">${renderSoap(c.soap || 'Генерация SOAP...')}</div>
      <div id="panel-dialog" style="display:none" class="dialog-content">
        ${renderDialog(c.messages || [])}
      </div>
      <div id="panel-analyze" style="display:none" class="analyze-panel">
        <div style="font-size:12px;color:#666;margin-bottom:2px;">Вставьте результаты анализов, описание рентгена, ЭКГ или других исследований — ИИ разберёт их в контексте жалоб пациента.</div>
        <textarea class="analyze-input" id="analyze-input" placeholder="ОАК: Hb 98 г/л, лейкоциты 12.4×10⁹/л, СОЭ 34...&#10;Рентген ОГК: усиление лёгочного рисунка..."></textarea>
        <button class="btn btn-approve" id="btn-analyze" onclick="analyzeResults(${JSON.stringify(sessionId)})">🔬 Интерпретировать</button>
        <div id="analyze-result" class="analyze-result"><div class="ar-placeholder">Результат анализа появится здесь.</div></div>
      </div>
    </div>

    <div class="card" id="actions-card">
      <div class="draft-label" style="display:flex;align-items:center;gap:8px;">
        Ответ родителю ${statusBadge}
      </div>
      <div class="quick-replies">
        ${QUICK_REPLIES.map(r => r.isAppt
          ? `<button class="btn-quick" onclick='openApptModal(${JSON.stringify(sessionId)})'>${r.label}</button>`
          : `<button class="btn-quick" onclick='applyQuickReply(${JSON.stringify(r.text)})'>${r.label}</button>`
        ).join('')}
      </div>
      <textarea class="draft-edit" id="draft-text">${escapeHtml(draft)}</textarea>
      <div class="btn-row">
        <button class="btn btn-approve" id="btn-approve" onclick="approveCase('${sessionId}')" title="Клавиша A">
          ✅ ${isApproved ? 'Отправить ещё раз' : 'Одобрить и отправить'}
        </button>
        <button class="btn btn-edit" onclick="focusDraft()">
          ✏️ Редактировать
        </button>
        <button class="btn btn-reject" onclick="toggleRejectForm()">
          ✗ Отклонить
        </button>
        <button class="btn btn-archive" onclick="archiveCase('${sessionId}')">
          🗄 Убрать
        </button>
      </div>
      <div class="reject-form" id="reject-form">
        <textarea id="reject-reason" rows="2" placeholder="Причина (необязательно)..."></textarea>
        <div class="reject-actions">
          <button class="btn btn-reject-confirm" onclick="rejectCase('${sessionId}')">Отклонить</button>
          <button class="btn btn-archive" onclick="toggleRejectForm()">Отмена</button>
        </div>
      </div>
    </div>
  `;
}

const QUICK_REPLIES = [
  { label: '🌡 Жаропонижающее', text: 'При температуре выше 38,5°С дайте парацетамол или ибупрофен по весу ребёнка. Обильное питьё, прохладный воздух в комнате. Если температура держится больше 3 дней или поднимается выше 39,5 — обратитесь снова.' },
  { label: '📅 Запись на приём', isAppt: true, text: '' },
  { label: '🚑 Вызвать скорую', text: 'Немедленно вызывайте скорую — 103. Не ждите.' },
  { label: '👀 Наблюдение дома', text: 'Продолжайте наблюдение дома. Обильное питьё, покой. Если состояние ухудшится или появятся новые симптомы — напишите сразу.' },
  { label: '💊 Антибиотик не нужен', text: 'На данном этапе антибиотик не показан — это вирусная инфекция, она пройдёт сама. Симптоматическое лечение и наблюдение.' },
  { label: '✅ Всё в норме', text: 'Данные не вызывают опасений — это типичная картина для возраста. Продолжайте обычный режим.' },
];

function applyQuickReply(text) {
  const el = document.getElementById('draft-text');
  if (!el) return;
  el.disabled = false;
  el.value = text;
  el.focus();
  const btn = document.getElementById('btn-approve');
  if (btn) btn.disabled = false;
}

function switchTab(tab) {
  activeTab = tab;
  document.getElementById('tab-soap').className = 'tab' + (tab === 'soap' ? ' active' : '');
  document.getElementById('tab-dialog').className = 'tab' + (tab === 'dialog' ? ' active' : '');
  document.getElementById('tab-analyze').className = 'tab' + (tab === 'analyze' ? ' active' : '');
  document.getElementById('panel-soap').style.display = tab === 'soap' ? 'block' : 'none';
  document.getElementById('panel-dialog').style.display = tab === 'dialog' ? 'flex' : 'none';
  document.getElementById('panel-analyze').style.display = tab === 'analyze' ? 'flex' : 'none';
}

function renderDialog(messages) {
  if (!messages || messages.length === 0) return '<div style="color:#555;font-size:13px">Диалог недоступен</div>';
  return messages.map(m => {
    const isParent = m.role === 'parent';
    const roleLabel = isParent ? 'Родитель' : 'Ассистент';
    return `<div class="d-msg ${isParent ? 'parent' : 'bot'}">
      <div class="role">${roleLabel}</div>
      ${escapeHtml(m.text).split('\\n').join('<br>')}
    </div>`;
  }).join('');
}

function analyzeResults(sessionId) {
  const inputEl = document.getElementById('analyze-input');
  const resultEl = document.getElementById('analyze-result');
  const btnEl = document.getElementById('btn-analyze');
  const labText = inputEl ? inputEl.value.trim() : '';
  if (!labText) { showToast('Вставьте результаты анализов'); return; }
  btnEl.disabled = true;
  btnEl.textContent = '⏳ Анализирую...';
  resultEl.innerHTML = '<div class="ar-placeholder">ИИ обрабатывает данные...</div>';
  fetch('/api/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId, lab_text: labText})
  })
  .then(r => r.json())
  .then(data => {
    btnEl.disabled = false;
    btnEl.textContent = '🔬 Интерпретировать';
    if (data.error) { resultEl.innerHTML = '<div class="ar-crit">Ошибка: ' + escapeHtml(data.error) + '</div>'; return; }
    resultEl.innerHTML = renderAnalysis(data.result || '');
  })
  .catch(err => {
    btnEl.disabled = false;
    btnEl.textContent = '🔬 Интерпретировать';
    resultEl.innerHTML = '<div class="ar-crit">Ошибка соединения</div>';
  });
}

function renderAnalysis(text) {
  const NL = String.fromCharCode(10);
  return text.split(NL).map(line => {
    const safe = escapeHtml(line);
    if (safe.startsWith('### ') || safe.startsWith('## ') || safe.startsWith('# ')) {
      const t = safe.replace(/^#+\s*/, '');
      return '<div class="ar-title">' + t + '</div>';
    }
    if (/[↑↓⬆⬇]|(высок|низк|выше нормы|ниже нормы|повышен|снижен)/i.test(safe)) {
      return '<div class="ar-warn">' + safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>') + '</div>';
    }
    if (/(!{2,}|⚠|критич|срочно|экстренно)/i.test(safe)) {
      return '<div class="ar-crit">' + safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>') + '</div>';
    }
    const bold = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    return bold ? bold + '<br>' : '<br>';
  }).join('');
}

function renderSoap(text) {
  const NL = String.fromCharCode(10);
  const lines = text.split(NL);
  return lines.map(line => {
    const safe = escapeHtml(line);
    if (safe.startsWith('# ')) return '<div class="soap-h1">' + safe.slice(2) + '</div>';
    if (safe.startsWith('## ')) return '<div class="soap-h2">' + safe.slice(3) + '</div>';
    if (safe.startsWith('### ')) return '<div class="soap-h2">' + safe.slice(4) + '</div>';
    const bold = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    return bold + '<br>';
  }).join('');
}

function extractDraft(soap) {
  // Ищем любой из вариантов заголовка черновика
  const markers = [
    'Черновик ответа родителю:',
    'Рекомендуемый ответ родителю:',
    'Ответ родителю:',
    '💬'
  ];
  let idx = -1;
  let markerLen = 0;
  for (const m of markers) {
    idx = soap.indexOf(m);
    if (idx !== -1) { markerLen = m.length; break; }
  }
  if (idx === -1) return '';
  let draft = soap.slice(idx + markerLen).trim();
  // Убираем кавычки
  draft = draft.replace(/^[«"']|[»"']$/g, '').trim();
  // Убираем markdown построчно
  const NL2 = String.fromCharCode(10);
  draft = draft.split(NL2).map(l => {
    l = l.replace(/^#+\s*/, '');
    l = l.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/\*([^*]+)\*/g, '$1');
    return l;
  }).join(NL2).trim();
  return draft;
}

function focusDraft() {
  const el = document.getElementById('draft-text');
  if (el) { el.focus(); el.selectionStart = el.selectionEnd = el.value.length; }
}

function toggleRejectForm() {
  const f = document.getElementById('reject-form');
  if (f) f.classList.toggle('show');
}

function approveCase(sessionId) {
  const draft = document.getElementById('draft-text')?.value?.trim() || '';
  if (!draft) { showToast('⚠️ Напишите ответ родителю перед отправкой'); focusDraft(); return; }
  fetch('/api/approve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId, message: draft})
  }).then(r => {
    if (!r.ok) { showToast('⚠️ Ошибка отправки: ' + r.status); return; }
    if (cases[sessionId]) cases[sessionId].status = 'approved';
    renderSidebar();
    selectCase(sessionId);
    showToast('✅ Ответ отправлен родителю');
  }).catch(() => showToast('⚠️ Нет связи с сервером'));
}

function rejectCase(sessionId) {
  const reason = document.getElementById('reject-reason')?.value?.trim() || '';
  fetch('/api/reject', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId, reason})
  }).then(r => {
    if (!r.ok) { showToast('⚠️ Ошибка: ' + r.status); return; }
    if (cases[sessionId]) cases[sessionId].status = 'rejected';
    renderSidebar();
    selectCase(sessionId);
    showToast('✗ Кейс отклонён');
  }).catch(() => showToast('⚠️ Нет связи с сервером'));
}

function archiveCase(sessionId) {
  if (cases[sessionId]) cases[sessionId].status = 'archived';
  if (activeSession === sessionId) {
    activeSession = null;
    document.getElementById('main-content').innerHTML = `
      <div class="empty-main">
        <div class="icon">📋</div>
        <div style="font-size:15px;font-weight:600;color:#666">Выберите запрос</div>
      </div>`;
  }
  renderSidebar();
}

// Горячие клавиши
document.addEventListener('keydown', (e) => {
  if (!activeSession) return;
  const tag = document.activeElement.tagName;
  if (tag === 'TEXTAREA' || tag === 'INPUT') return;
  if (e.key === 'a' || e.key === 'A') { e.preventDefault(); approveCase(activeSession); }
  if (e.key === 'Escape') { archiveCase(activeSession); }
});

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 3500);
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function reloadSessions() {
  const pollEl = document.getElementById('poll-status');
  const statusEl = document.getElementById('ws-status');
  fetch('/api/sessions?t=' + Date.now())
    .then(r => {
      if (!r.ok) {
        if (pollEl) pollEl.textContent = '⚠️ Сервер ' + r.status;
        if (statusEl) { statusEl.textContent = '🔴 Ошибка ' + r.status; statusEl.style.color = '#f44336'; }
        return null;
      }
      if (statusEl) { statusEl.textContent = '🟢 Активен'; statusEl.style.color = '#4caf50'; }
      return r.json();
    })
    .then(list => {
      if (!list) return;
      const t = new Date().toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
      let newCount = 0;
      list.forEach(c => {
        const isNew = !cases[c.session_id];
        cases[c.session_id] = c;
        if (isNew) {
          newCount++;
          renderSidebar();
          if (!activeSession) {
            try { selectCase(c.session_id); } catch(e) { console.error('selectCase error:', e); }
          }
        }
      });
      renderSidebar();
      const total = Object.values(cases).filter(c => c.status !== 'archived').length;
      if (pollEl) pollEl.textContent = t + (total > 0 ? ' · ' + total + ' запр.' : ' · пусто');
      if (newCount > 0) showToast('🔔 Новых запросов: ' + newCount);
    })
    .catch(err => {
      console.error('[poll] error:', err);
      if (pollEl) pollEl.textContent = '⚠️ Нет связи';
      if (statusEl) { statusEl.textContent = '🔴 Нет связи'; statusEl.style.color = '#f44336'; }
    });
}

connect();

// BFCache: если Safari вернул страницу из кеша — принудительно перезагружаем
window.addEventListener('pageshow', (e) => {
  if (e.persisted) location.reload();
});

// ─── Запись на приём ──────────────────────────────────────────────────────

function openApptModal(sessionId) {
  const c = cases[sessionId];
  const labelEl = document.getElementById('appt-patient-line');
  if (labelEl) labelEl.textContent = c ? c.label : '';
  // Дата по умолчанию — завтра
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  const iso = tomorrow.toISOString().slice(0, 10);
  document.getElementById('appt-date').value = iso;
  document.getElementById('appt-time').value = '10:00';
  document.getElementById('appt-note').value = '';
  document.getElementById('appt-modal').dataset.sid = sessionId || '';
  document.getElementById('appt-modal').classList.remove('hidden');
}

function closeApptModal() {
  document.getElementById('appt-modal').classList.add('hidden');
}

function confirmAppointment() {
  const sid = document.getElementById('appt-modal').dataset.sid;
  const date = document.getElementById('appt-date').value;
  const time = document.getElementById('appt-time').value;
  const note = document.getElementById('appt-note').value.trim();
  if (!date || !time) { showToast('⚠️ Выберите дату и время'); return; }
  const label = (cases[sid] && cases[sid].label) || 'Пациент';
  fetch('/api/appointment', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sid, label, date, time, note})
  }).then(r => r.json()).then(res => {
    if (!res.ok) { showToast('⚠️ Ошибка сохранения'); return; }
    closeApptModal();
    // Заполняем черновик ответа родителю
    const dateStr = formatDateRu(date);
    const draftText = 'Записываю вас на приём: ' + dateStr + ' в ' + time + '.' +
      (note ? ' ' + note + '.' : '') + ' Врач будет вас ждать. Если планы изменятся — напишите заранее.';
    const ta = document.getElementById('draft-text');
    if (ta) { ta.value = draftText; ta.focus(); }
    showToast('✅ Запись сохранена: ' + dateStr + ' ' + time);
    calWeekStart = getMonday(new Date(date + 'T12:00:00'));
    if (document.getElementById('schedule-panel').classList.contains('open')) {
      renderCalendar();
    }
  }).catch(() => showToast('⚠️ Нет связи'));
}

function formatDateRu(iso) {
  const d = new Date(iso + 'T12:00:00');
  const months = ['января','февраля','марта','апреля','мая','июня',
                  'июля','августа','сентября','октября','ноября','декабря'];
  const days = ['вс','пн','вт','ср','чт','пт','сб'];
  return d.getDate() + ' ' + months[d.getMonth()] + ' (' + days[d.getDay()] + ')';
}

// ─── Панель-календарь (слайд справа) ─────────────────────────────────────

let calWeekStart = null;

function getMonday(d) {
  const day = d.getDay();
  const result = new Date(d);
  result.setDate(d.getDate() - day + (day === 0 ? -6 : 1));
  result.setHours(0, 0, 0, 0);
  return result;
}

function toggleSchedule() {
  const panel = document.getElementById('schedule-panel');
  const opening = !panel.classList.contains('open');
  panel.classList.toggle('open');
  if (opening) {
    if (!calWeekStart) calWeekStart = getMonday(new Date());
    renderCalendar();
  }
}

function calPrevWeek() {
  calWeekStart = new Date(calWeekStart);
  calWeekStart.setDate(calWeekStart.getDate() - 7);
  renderCalendar();
}

function calNextWeek() {
  calWeekStart = new Date(calWeekStart);
  calWeekStart.setDate(calWeekStart.getDate() + 7);
  renderCalendar();
}

function calGoToday() {
  calWeekStart = getMonday(new Date());
  renderCalendar();
}

function renderCalendar() {
  fetch('/api/appointments').then(r => r.json()).then(list => {
    const TODAY = new Date();
    TODAY.setHours(0, 0, 0, 0);
    const todayStr = TODAY.toISOString().slice(0, 10);

    const MONTH_RU = ['янв','фев','мар','апр','май','июн',
                      'июл','авг','сен','окт','ноя','дек'];
    const MONTH_FULL = ['января','февраля','марта','апреля','мая','июня',
                        'июля','августа','сентября','октября','ноября','декабря'];
    const DAY_RU = ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];

    const days = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(calWeekStart);
      d.setDate(calWeekStart.getDate() + i);
      days.push(d);
    }

    // Заголовок «16–22 мар 2026»
    const d0 = days[0], d6 = days[6];
    let title = d0.getDate();
    if (d0.getMonth() !== d6.getMonth()) title += ' ' + MONTH_RU[d0.getMonth()];
    title += '\u2013' + d6.getDate() + ' ' + MONTH_RU[d6.getMonth()] + ' ' + d6.getFullYear();
    document.getElementById('cal-nav-title').textContent = title;

    // Индекс: "YYYY-MM-DD|HH" → [appt, ...]
    const idx = {};
    list.forEach(a => {
      const h = parseInt(a.time.split(':')[0], 10);
      const k = a.date + '|' + h;
      if (!idx[k]) idx[k] = [];
      idx[k].push(a);
    });

    // Шапка
    const thCells = days.map(d => {
      const ds = d.toISOString().slice(0, 10);
      const isToday = ds === todayStr;
      const num = isToday
        ? '<span class="cal-day-num today-num">' + d.getDate() + '</span>'
        : '<span class="cal-day-num">' + d.getDate() + '</span>';
      return '<th class="' + (isToday ? 'today-col' : '') + '">'
        + '<span class="cal-day-name">' + DAY_RU[d.getDay()] + '</span>'
        + num + '</th>';
    }).join('');

    // Строки 9:00–19:00
    const rows = [];
    for (let h = 9; h <= 19; h++) {
      const cells = days.map(d => {
        const ds = d.toISOString().slice(0, 10);
        const isToday = ds === todayStr;
        const appts = idx[ds + '|' + h] || [];
        const inner = appts.map(a => [
          '<div class="cal-appt">',
          '<button class="cal-appt-del" onclick="deleteAppt(this.dataset.id)" data-id="' + escapeHtml(String(a.id)) + '" title="Удалить">✕</button>',
          '<div class="cal-appt-time">' + a.time + '</div>',
          '<div class="cal-appt-name">' + escapeHtml(a.label) + '</div>',
          (a.note ? '<div class="cal-appt-note">' + escapeHtml(a.note) + '</div>' : ''),
          '</div>'
        ].join('')).join('');
        return '<td class="' + (isToday ? 'today-col' : '') + '">' + inner + '</td>';
      }).join('');
      rows.push('<tr><td class="cal-time-cell">' + h + ':00</td>' + cells + '</tr>');
    }

    document.getElementById('schedule-body').innerHTML =
      '<table class="cal-table"><thead><tr>'
      + '<th style="width:38px"></th>' + thCells
      + '</tr></thead><tbody>' + rows.join('') + '</tbody></table>';

  }).catch(() => {});
}

function deleteAppt(btn) {
  const id = (typeof btn === 'string') ? btn : btn.dataset.id;
  fetch('/api/appointment/' + id, {method: 'DELETE'})
    .then(r => r.json()).then(() => { renderCalendar(); showToast('Запись удалена'); });
}

// Закрываем модал по клику вне него
document.getElementById('appt-modal').addEventListener('click', function(e) {
  if (e.target === this) closeApptModal();
});
</script>
</body></html>"""


@app.get("/api/sessions")
async def api_sessions():
    """REST эндпоинт для кокпита — получить все активные сессии."""
    from fastapi.responses import JSONResponse
    result = []
    for sid, s in sessions.items():
        if s.get("status") in ("waiting_doctor", "approved", "rejected"):
            result.append({
                "type": "new_case",
                "session_id": sid,
                "label": extract_patient_label(s),
                "preview": (s.get("messages") or [{"text": ""}])[-1].get("text", "")[:60],
                "soap": s.get("soap"),
                "time": s.get("created_at", ""),
                "status": s.get("status"),
                "messages": [{"role": m["role"], "text": m["text"]} for m in s.get("messages", [])],
            })
    return JSONResponse(result)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _check_session(request):
        return RedirectResponse("/cockpit", status_code=302)
    return HTMLResponse(_LOGIN_HTML.replace("ERROR_PLACEHOLDER", ""))


@app.post("/login")
async def login_submit(request: Request, response: Response, password: str = Form(...)):
    ok = secrets.compare_digest(password.encode(), COCKPIT_PASSWORD.encode())
    if not ok:
        error = '<p class="error">Неверный пароль. Попробуйте ещё раз.</p>'
        return HTMLResponse(_LOGIN_HTML.replace("ERROR_PLACEHOLDER", error))
    token = secrets.token_urlsafe(32)
    _cockpit_sessions.add(token)
    # ?v= гарантирует что Safari не возьмёт кокпит из кеша
    resp = RedirectResponse(f"/cockpit?v={SERVER_START}", status_code=302)
    resp.set_cookie("cockpit_session", token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("cockpit_session")
    _cockpit_sessions.discard(token)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("cockpit_session")
    return resp


@app.get("/api/events")
async def sse_events(request: Request):
    """SSE-поток для кокпита врача. Работает в Safari (в отличие от WebSocket)."""
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)

    queue: asyncio.Queue = asyncio.Queue()
    sse_queues.append(queue)

    # Текущие сессии — отправляем сразу при подключении
    existing = []
    for sid, s in sessions.items():
        if s.get("status") in ("waiting_doctor", "approved", "rejected"):
            existing.append({
                "type": "new_case",
                "session_id": sid,
                "label": extract_patient_label(s),
                "preview": (s.get("messages") or [{"text": ""}])[-1].get("text", "")[:60],
                "soap": s.get("soap"),
                "time": s.get("created_at", ""),
                "status": s.get("status"),
                "messages": [{"role": m["role"], "text": m["text"]} for m in s.get("messages", [])],
            })

    async def generator():
        try:
            # Сразу шлём все текущие кейсы
            if existing:
                yield f"data: {json.dumps({'type': 'init', 'cases': existing}, ensure_ascii=False)}\n\n"
            # Слушаем очередь
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # не даём браузеру закрыть соединение
        finally:
            if queue in sse_queues:
                sse_queues.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/approve")
async def api_approve(request: Request):
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    data = await request.json()
    sid = data.get("session_id", "")
    approved_msg = data.get("message", "")
    if sid not in sessions:
        return Response("Not found", status_code=404)
    sessions[sid]["status"] = "approved"
    sessions[sid]["messages"].append({"role": "doctor", "text": approved_msg, "time": datetime.now().isoformat()})
    parent_socket = parent_ws.get(sid)
    if parent_socket:
        try:
            await parent_socket.send_text(json.dumps({"type": "doctor_reply", "text": f"👨‍⚕️ {approved_msg}"}))
        except Exception:
            pass
    await notify_cockpit({"type": "case_update", "session_id": sid, "status": "approved"})
    return {"ok": True}


@app.post("/api/reject")
async def api_reject(request: Request):
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    data = await request.json()
    sid = data.get("session_id", "")
    if sid not in sessions:
        return Response("Not found", status_code=404)
    sessions[sid]["status"] = "rejected"
    await notify_cockpit({"type": "case_update", "session_id": sid, "status": "rejected"})
    return {"ok": True}


@app.post("/api/appointment")
async def api_appointment(request: Request):
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    data = await request.json()
    appt = {
        "id": secrets.token_hex(6),
        "session_id": data.get("session_id", ""),
        "label": data.get("label", "Пациент"),
        "date": data.get("date", ""),
        "time": data.get("time", ""),
        "note": data.get("note", ""),
    }
    appointments.append(appt)
    appointments.sort(key=lambda a: (a["date"], a["time"]))
    return {"ok": True, "id": appt["id"]}


@app.get("/api/appointments")
async def api_appointments(request: Request):
    from fastapi.responses import JSONResponse
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    return JSONResponse(appointments)


@app.delete("/api/appointment/{appt_id}")
async def api_delete_appointment(appt_id: str, request: Request):
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    global appointments
    appointments = [a for a in appointments if a["id"] != appt_id]
    return {"ok": True}


@app.post("/api/analyze")
async def api_analyze(request: Request):
    """Интерпретация анализов — Claude разбирает результаты в контексте SOAP-заметки."""
    if not _check_session(request):
        return Response("Unauthorized", status_code=401)
    data = await request.json()
    session_id = data.get("session_id", "")
    lab_text = data.get("lab_text", "").strip()
    if not lab_text:
        return {"error": "Нет данных для анализа"}

    session = sessions.get(session_id, {})
    soap = session.get("soap", "")

    if DEMO_MODE:
        await asyncio.sleep(1.5)
        return {"result": """## Общий анализ крови

**Гемоглобин 98 г/л** — ↓ ниже нормы (норма 115–145 г/л для данного возраста). Лёгкая железодефицитная анемия.
**Лейкоциты 12.4×10⁹/л** — ↑ умеренный лейкоцитоз. Характерен для бактериальной инфекции или вирусного воспаления.
**СОЭ 34 мм/ч** — ↑ повышено. Подтверждает активный воспалительный процесс.

## Клиническое значение

В сочетании с жалобами пациента: картина соответствует острому инфекционному процессу на фоне латентного железодефицита.

## Рекомендации врачу

- Рассмотреть дополнительно: ферритин, сывороточное железо, TIBC
- При подтверждении ЖДА — препараты железа после купирования острого процесса
- Контроль ОАК через 2–3 недели"""}

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        context_block = ""
        if soap:
            context_block = f"""SOAP-заметка пациента (контекст):
{soap}

"""

        prompt = f"""{context_block}Результаты исследований, предоставленные пациентом:
{lab_text}

Задача: интерпретируй результаты как клинический ассистент педиатра.

Структура ответа (используй Markdown ##):
1. Разбор каждого показателя — норма/отклонение, клиническое значение
2. Общая картина — что всё это означает в контексте жалоб
3. На что обратить внимание врачу — какие дополнительные данные нужны, какие шаги рассмотреть

Пиши кратко, по делу, для врача. Отклонения обозначай стрелками ↑↓."""

        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system="Ты клинический ассистент педиатра. Интерпретируй результаты исследований точно и кратко на русском языке. Указывай нормы для детского возраста. Не ставь диагноз — только помогай врачу увидеть клиническую картину.",
            messages=[{"role": "user", "content": prompt}],
        )
        return {"result": response.content[0].text}
    except Exception as e:
        return {"error": str(e)}


@app.get("/cockpit", response_class=HTMLResponse)
async def cockpit_page(request: Request):
    if not _check_session(request):
        return RedirectResponse("/login", status_code=302)
    # Если версия в URL не совпадает с текущей — редиректим на актуальный кокпит
    v = request.query_params.get("v")
    if v and v != str(SERVER_START):
        return RedirectResponse(f"/cockpit?v={SERVER_START}", status_code=302)
    if not v:
        return RedirectResponse(f"/cockpit?v={SERVER_START}", status_code=302)
    mode = "⚠️ DEMO" if DEMO_MODE else "✅ Claude AI"
    cls = "mode-demo" if DEMO_MODE else "mode-real"
    html = (COCKPIT_HTML
            .replace("MODE_PLACEHOLDER", mode)
            .replace("MODE_CLASS", cls)
            .replace("SERVER_VERSION_PLACEHOLDER", str(SERVER_START)))
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.websocket("/ws/cockpit")
async def cockpit_websocket(websocket: WebSocket):
    await websocket.accept()
    cockpit_ws.append(websocket)

    existing = []
    for sid, s in sessions.items():
        if s.get("status") in ("waiting_doctor", "approved", "rejected"):
            existing.append({
                "type": "new_case",
                "session_id": sid,
                "label": extract_patient_label(s),
                "preview": (s.get("messages") or [{"text": ""}])[-1].get("text", "")[:60],
                "soap": s.get("soap"),
                "time": s.get("created_at", ""),
                "status": s.get("status"),
                "messages": [
                    {"role": m["role"], "text": m["text"]}
                    for m in s.get("messages", [])
                ],
            })
    if existing:
        await websocket.send_text(json.dumps({"type": "init", "cases": existing}, ensure_ascii=False))

    try:
        while True:
            data = json.loads(await websocket.receive_text())

            if data.get("type") == "approve":
                sid = data["session_id"]
                approved_msg = data.get("message", "")
                if sid in sessions:
                    sessions[sid]["status"] = "approved"
                    sessions[sid]["messages"].append({"role": "doctor", "text": approved_msg, "time": datetime.now().isoformat()})
                    parent_socket = parent_ws.get(sid)
                    if parent_socket:
                        try:
                            await parent_socket.send_text(json.dumps({
                                "type": "doctor_reply",
                                "text": f"👨‍⚕️ {approved_msg}"
                            }))
                        except Exception:
                            pass
                    await notify_cockpit({"type": "case_update", "session_id": sid, "status": "approved"})

            elif data.get("type") == "reject":
                sid = data["session_id"]
                if sid in sessions:
                    sessions[sid]["status"] = "rejected"
                    await notify_cockpit({"type": "case_update", "session_id": sid, "status": "rejected"})

    except WebSocketDisconnect:
        if websocket in cockpit_ws:
            cockpit_ws.remove(websocket)


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("  🎻 Doc Orchestra MVP")
    print("=" * 55)
    print(f"  Режим: {'⚠️  DEMO (без Claude)' if DEMO_MODE else '✅ REAL (Claude подключён)'}")
    print()
    print("  Открой в браузере:")
    print("  📱 Родитель  →  http://localhost:8081/parent")
    print("  🩺 Врач      →  http://localhost:8081/cockpit")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
