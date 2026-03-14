"""
Doc Orchestra — MVP Demo
========================
Запуск: python mvp.py
Затем открой в браузере:
  Родитель : http://localhost:8080/parent
  Врач     : http://localhost:8080/cockpit

Работает в двух режимах:
  DEMO  — без API ключа, с реалистичными заготовленными ответами
  REAL  — с GEMINI_API_KEY в .env файле, настоящий ИИ
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# --------------------------------------------------------------------------
# Загрузка .env если есть
# --------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEMO_MODE = not bool(ANTHROPIC_API_KEY)

if DEMO_MODE:
    print("\n⚠️  DEMO MODE — ANTHROPIC_API_KEY не найден.")
    print("    Добавь ANTHROPIC_API_KEY= в файл .env\n")
else:
    print(f"\n✅ REAL MODE — Claude API подключён.\n")

# --------------------------------------------------------------------------
# Хранилище в памяти (для MVP, без БД)
# --------------------------------------------------------------------------
sessions: dict = {}       # session_id → {messages, soap, status, phase}
cockpit_ws: list = []     # подключённые WebSocket врача
parent_ws: dict = {}      # session_id → WebSocket родителя

# --------------------------------------------------------------------------
# ИИ — Gemini или Demo
# --------------------------------------------------------------------------

DEMO_RESPONSES = {
    "greeting": (
        "Здравствуйте! Я медицинский ассистент клиники. "
        "Расскажите, пожалуйста, что беспокоит вашего ребёнка?"
    ),
    "complaint": (
        "Понимаю, это беспокоит. Как давно появились эти симптомы? "
        "И есть ли у ребёнка температура?"
    ),
    "details": (
        "Хорошо, я всё записал. Уточните, пожалуйста — какой вес ребёнка "
        "и были ли похожие эпизоды раньше?"
    ),
    "waiting": (
        "Спасибо, я передал всю информацию доктору. "
        "Он рассмотрит ваш запрос и ответит в ближайшее время. "
        "Если состояние ухудшится или появится затруднённое дыхание, "
        "потеря сознания — немедленно вызывайте скорую помощь (103)."
    ),
}

DEMO_SOAP = """
📋 SOAP ЗАМЕТКА — {name} ({age}, {sex})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

S — Субъективно:
  Жалобы: {complaint}
  Длительность: {duration}
  Сопутствующие симптомы: слабость, снижение аппетита
  Температура: субфебрильная 37.3°C

O — Объективно:
  Вес: {weight} кг
  Бледность кожных покровов
  Лабораторные данные: ожидают загрузки отчёта

A — Оценка:
  Рабочий диагноз: Железодефицитная анемия (предположительно)
  Дифференциальный: Анемия хронических заболеваний
  ⚠️ Красный флаг: Hb ниже целевого для возраста — требует уточнения

P — План:
  1-я линия: ОАК с ретикулоцитами, ферритин, CRP
  Препараты: Феррум Лек, ожидает одобрения врача
  Наблюдение: повтор ОАК через 4 недели

💬 Черновик ответа родителю:
  «Доктор ознакомился с информацией. Необходимо сдать анализы крови:
  ОАК + ферритин + CRP. После результатов назначим лечение.
  При нарастании бледности или вялости — обратитесь сразу.»
"""


def _demo_smart_response(session_id: str, user_message: str, phase: str) -> str:
    """
    Контекстно-умный demo ответ.
    Читает что написал родитель и не повторяет вопросы на которые уже ответили.
    """
    session = sessions.get(session_id, {})
    all_parent_msgs = " ".join(
        m["text"].lower() for m in session.get("messages", []) if m["role"] == "parent"
    ) + " " + user_message.lower()
    bot_msgs = [m["text"] for m in session.get("messages", []) if m["role"] == "bot"]
    last_bot = bot_msgs[-1] if bot_msgs else ""
    msg_lower = user_message.lower()

    # --- Определяем что родитель уже рассказал ---
    told_temp     = any(w in all_parent_msgs for w in ["температур", "жар", "горит", "38", "39", "40", "37"])
    told_duration = any(w in all_parent_msgs for w in ["минут", "час", "день", "дней", "давно", "сегодня", "вчера", "неделю"])
    told_weight   = any(w in all_parent_msgs for w in ["кг", "килограмм", "вес "])
    told_rash     = any(w in all_parent_msgs for w in ["сыпь", "пятн", "краснот", "высыпан", "точки"])
    told_age      = any(w in all_parent_msgs for w in ["лет", "месяц", "год", "годик"])

    # --- Детектируем раздражение / возражение родителя ---
    frustrated = any(w in msg_lower for w in [
        "при чем", "зачем", "я же", "уже говор", "уже сказ", "почему вы",
        "это важно", "не понимаю", "странный вопрос", "не отвечаете"
    ])

    # --- Детектируем экстренную ситуацию ---
    emergency = any(w in all_parent_msgs for w in [
        "не дышит", "без сознания", "судорог", "синеет", "задыхается",
        "потерял сознание", "очень тяжело", "скорую", "104", "105"
    ])

    if emergency:
        return (
            "⚠️ Это требует НЕМЕДЛЕННОЙ помощи!\n\n"
            "Пожалуйста, прямо сейчас вызовите скорую помощь — 103.\n\n"
            "Пока едет скорая:\n"
            "• Уложите ребёнка на бок\n"
            "• Не оставляйте одного\n"
            "• Расстегните одежду\n\n"
            "Врач уведомлён о срочном обращении."
        )

    if frustrated:
        # Извиняемся и объясняем зачем нужна информация
        if "вес" in last_bot:
            return (
                "Понимаю ваше беспокойство, и прошу прощения за непонятный вопрос. "
                "Вес ребёнка нужен врачу для точного расчёта дозировки лекарств, если они понадобятся. "
                "Но если сейчас не знаете точно — не переживайте, можно пропустить. "
                "Скажите лучше: это первый раз когда появилась такая температура и сыпь, "
                "или подобное уже было раньше?"
            )
        return (
            "Прошу прощения, я понимаю как это волнительно. "
            "Позвольте уточнить самое важное для врача — "
            "есть ли у ребёнка затруднённое дыхание или сыпь не бледнеет при надавливании?"
        )

    if phase == "greeting":
        return DEMO_RESPONSES["greeting"]

    if phase == "complaint":
        # Не спрашиваем про температуру если уже знаем
        if told_temp and told_rash:
            if told_duration:
                return (
                    "Понял, записал. Температура и сыпь на лице — это важные симптомы. "
                    "Уточните: сыпь бледнеет если нажать на неё пальцем? "
                    "И ребёнок сейчас активен или вялый?"
                )
            return (
                "Записал. Как давно появилась температура — несколько часов или с утра?"
            )
        if told_temp and not told_rash:
            return (
                "Понял, температура уже записана. "
                "Есть ли какие-то высыпания на коже, горле или во рту?"
            )
        if told_rash and not told_temp:
            return (
                "Сыпь записал. Есть ли у ребёнка температура прямо сейчас? "
                "Если да — сколько градусов?"
            )
        return DEMO_RESPONSES["complaint"]

    if phase == "details":
        # Не спрашиваем про вес если уже рассказали, ищем что ещё нужно
        questions = []
        if not told_weight:
            questions.append("вес ребёнка")
        if not told_age:
            questions.append("точный возраст")

        if not questions:
            # Всё собрали — переходим к ожиданию
            return (
                "Отлично, вся необходимая информация собрана. "
                "Передаю доктору — он ответит в ближайшее время. "
                "Если состояние ухудшится — вызывайте скорую 103."
            )

        q = questions[0]
        return (
            f"Хорошо, это важная информация. "
            f"Последнее что нужно врачу — {q}. Можете уточнить?"
        )

    return DEMO_RESPONSES["waiting"]


async def get_ai_response(session_id: str, user_message: str, phase: str) -> str:
    if DEMO_MODE:
        await asyncio.sleep(1.2)  # имитация задержки
        return _demo_smart_response(session_id, user_message, phase)

    # REAL MODE — Claude
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        session = sessions.get(session_id, {})

        # Строим историю диалога для Claude
        messages = []
        for m in session.get("messages", [])[-12:]:
            role = "user" if m["role"] == "parent" else "assistant"
            messages.append({"role": role, "content": m["text"]})

        # Добавляем текущее сообщение родителя
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": user_message})
        else:
            # Если последнее уже user — это первое сообщение (приветствие без текста)
            messages[-1]["content"] = user_message

        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system="""Ты — медицинский ассистент педиатрической клиники. Твоя задача: собрать информацию о состоянии ребёнка и передать её врачу.

ПРАВИЛА (строго обязательны):
1. Отвечай тепло, с эмпатией, на том языке на котором пишет родитель.
2. НИКОГДА не ставь диагноз и не предлагай лечение — только собирай информацию.
3. Задавай ОДИН конкретный вопрос за раз.
4. Не повторяй вопросы на которые уже ответили.
5. Внимательно читай каждое сообщение — родитель мог уже ответить на твой вопрос.
6. Если родитель раздражён или тревожится — сначала успокой, потом спрашивай.
7. Экстренная ситуация (не дышит, судороги, потеря сознания) → НЕМЕДЛЕННО: "Вызовите скорую 103 прямо сейчас!"
8. Когда соберёшь: жалобу, длительность, основные симптомы — скажи что передаёшь врачу.

Ты НЕ врач. Ты помощник который помогает врачу быстро понять ситуацию.""",
            messages=messages,
        )
        return response.content[0].text
    except Exception as e:
        return f"Извините, произошла техническая ошибка. Пожалуйста, обратитесь к врачу напрямую. ({e})"


async def generate_soap(session_id: str) -> str:
    session = sessions.get(session_id, {})
    msgs = session.get("messages", [])

    if DEMO_MODE:
        await asyncio.sleep(2)
        parent_msgs = " ".join(m["text"] for m in msgs if m["role"] == "parent")
        complaint = parent_msgs[:80] + "..." if len(parent_msgs) > 80 else parent_msgs
        return DEMO_SOAP.format(
            name="Пациент",
            age="4 года",
            sex="Ж",
            complaint=complaint,
            duration="~2 недели",
            weight="14.2",
        )

    # REAL MODE — Claude генерирует SOAP
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
            system="Ты клинический ассистент педиатра. Составляй краткие структурированные SOAP заметки на русском языке для врача.",
            messages=[{"role": "user", "content": f"""На основе диалога с родителем составь SOAP заметку для врача-педиатра.

ДИАЛОГ:
{history_text}

Формат ответа:
📋 SOAP ЗАМЕТКА
━━━━━━━━━━━━━━━━━━━━━━

S — Субъективно:
  (жалобы со слов родителя, длительность, динамика)

O — Объективно:
  (симптомы, данные которые сообщил родитель — температура, сыпь и т.д.)

A — Предварительная оценка:
  (возможные диагнозы для рассмотрения врачом, красные флаги если есть)

P — Рекомендуемые действия:
  (какие анализы/осмотр нужны, что уточнить)

💬 Черновик ответа родителю:
  (тёплый, эмпатичный текст БЕЗ диагноза, что делать пока ждёт врача)"""}],
        )
        return response.content[0].text
    except Exception as e:
        return f"Ошибка генерации SOAP: {e}"


# --------------------------------------------------------------------------
# Логика сессии
# --------------------------------------------------------------------------

def get_or_create_session(session_id: str) -> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "id": session_id,
            "messages": [],
            "soap": None,
            "status": "active",   # active | waiting_doctor | approved | sent
            "phase": "greeting",
            "created_at": datetime.now().strftime("%H:%M"),
        }
    return sessions[session_id]


def advance_phase(session: dict) -> str:
    """Переводит диалог через фазы по количеству сообщений родителя."""
    parent_count = sum(1 for m in session["messages"] if m["role"] == "parent")
    if parent_count <= 1:
        return "complaint"
    elif parent_count <= 3:
        return "details"
    else:
        return "waiting"


async def notify_cockpit(event: dict):
    """Отправить обновление всем подключённым кокпитам врача."""
    dead = []
    for ws in cockpit_ws:
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            dead.append(ws)
    for ws in dead:
        cockpit_ws.remove(ws)


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="Doc Orchestra MVP")


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;max-width:600px;margin:60px auto;text-align:center">
    <h1>🎻 Doc Orchestra MVP</h1>
    <p>Демонстрация системы асинхронного ведения пациентов</p>
    <div style="display:flex;gap:20px;justify-content:center;margin-top:40px">
      <a href="/parent" style="padding:20px 40px;background:#25D366;color:white;border-radius:12px;text-decoration:none;font-size:18px">
        📱 Интерфейс родителя
      </a>
      <a href="/cockpit" style="padding:20px 40px;background:#2563eb;color:white;border-radius:12px;text-decoration:none;font-size:18px">
        🩺 Кокпит врача
      </a>
    </div>
    <p style="margin-top:30px;color:#888">{'⚠️ DEMO MODE' if DEMO_MODE else '✅ Gemini подключён'}</p>
    </body></html>
    """.replace("{'⚠️ DEMO MODE' if DEMO_MODE else '✅ Gemini подключён'}",
                "⚠️ DEMO MODE — без Gemini API" if DEMO_MODE else "✅ Gemini API подключён"))


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
body { font-family: -apple-system, sans-serif; background: #e5ddd5; height: 100vh; display: flex; flex-direction: column; }
.header { background: #075e54; color: white; padding: 16px 20px; display: flex; align-items: center; gap: 12px; }
.avatar { width: 42px; height: 42px; background: #25D366; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; }
.header-text h3 { font-size: 16px; } .header-text p { font-size: 12px; opacity: 0.8; }
.messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
.msg { max-width: 75%; padding: 8px 12px; border-radius: 8px; font-size: 14px; line-height: 1.5; position: relative; }
.msg.bot { background: white; align-self: flex-start; border-radius: 0 8px 8px 8px; }
.msg.user { background: #dcf8c6; align-self: flex-end; border-radius: 8px 0 8px 8px; }
.msg.system { background: #fff3cd; align-self: center; font-size: 12px; color: #856404; border-radius: 8px; padding: 6px 12px; max-width: 90%; text-align: center; }
.msg .time { font-size: 10px; color: #999; text-align: right; margin-top: 4px; }
.typing { background: white; padding: 12px; border-radius: 0 8px 8px 8px; display: inline-flex; gap: 4px; align-self: flex-start; }
.typing span { width: 8px; height: 8px; background: #999; border-radius: 50%; animation: bounce 1.2s infinite; }
.typing span:nth-child(2) { animation-delay: 0.2s; } .typing span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-8px)} }
.input-area { background: #f0f0f0; padding: 12px 16px; display: flex; gap: 8px; align-items: center; }
textarea { flex: 1; padding: 10px 14px; border: none; border-radius: 24px; resize: none; font-size: 14px; outline: none; max-height: 100px; }
button { width: 44px; height: 44px; background: #25D366; border: none; border-radius: 50%; cursor: pointer; color: white; font-size: 20px; display: flex; align-items: center; justify-content: center; }
button:disabled { background: #ccc; }
.badge { background: #ff3b30; color: white; border-radius: 50%; padding: 2px 6px; font-size: 11px; }
</style>
</head>
<body>
<div class="header">
  <div class="avatar">🩺</div>
  <div class="header-text">
    <h3>Медицинский ассистент</h3>
    <p id="status">Онлайн • Отвечает мгновенно</p>
  </div>
</div>
<div class="messages" id="messages"></div>
<div class="input-area">
  <textarea id="input" placeholder="Напишите сообщение..." rows="1" onkeydown="handleKey(event)"></textarea>
  <button id="sendBtn" onclick="sendMessage()">➤</button>
</div>

<script>
const SESSION_ID = 'SESSION_PLACEHOLDER';
let ws;
let isWaiting = false;

function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws/parent/' + SESSION_ID);
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'message') addMessage(data.text, 'bot');
    else if (data.type === 'typing') showTyping(data.show);
    else if (data.type === 'status_update') {
      document.getElementById('status').textContent = data.text;
      if (data.system_msg) addMessage(data.system_msg, 'system');
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

function addMessage(text, role) {
  const div = document.getElementById('messages');
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  const time = new Date().toLocaleTimeString('ru', {hour:'2-digit', minute:'2-digit'});
  el.innerHTML = text.replace(/\\n/g, '<br>') + '<div class="time">' + time + '</div>';
  div.appendChild(el);
  div.scrollTop = div.scrollHeight;
  removeTyping();
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

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text || isWaiting) return;
  addMessage(text, 'user');
  ws.send(JSON.stringify({type: 'message', text: text}));
  input.value = '';
  isWaiting = true;
  document.getElementById('sendBtn').disabled = true;
  setTimeout(() => { isWaiting = false; document.getElementById('sendBtn').disabled = false; }, 3000);
}

connect();
</script>
</body></html>"""


@app.get("/parent", response_class=HTMLResponse)
async def parent_page():
    session_id = str(uuid.uuid4())[:8]
    return HTMLResponse(PARENT_HTML.replace("SESSION_PLACEHOLDER", session_id))


@app.websocket("/ws/parent/{session_id}")
async def parent_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    parent_ws[session_id] = websocket
    session = get_or_create_session(session_id)

    # Отправить приветствие
    await websocket.send_text(json.dumps({"type": "typing", "show": True}))
    greeting = await get_ai_response(session_id, "", "greeting")
    await websocket.send_text(json.dumps({"type": "typing", "show": False}))
    await websocket.send_text(json.dumps({"type": "message", "text": greeting}))
    session["messages"].append({"role": "bot", "text": greeting, "time": datetime.now().isoformat()})

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            if data.get("type") != "message":
                continue

            user_text = data["text"]
            session["messages"].append({"role": "parent", "text": user_text, "time": datetime.now().isoformat()})

            # Показать индикатор набора
            await websocket.send_text(json.dumps({"type": "typing", "show": True}))

            phase = advance_phase(session)
            session["phase"] = phase

            if phase == "waiting":
                # Достаточно инфо — генерируем SOAP и отправляем врачу
                bot_reply = DEMO_RESPONSES["waiting"] if DEMO_MODE else await get_ai_response(session_id, user_text, phase)
                await websocket.send_text(json.dumps({"type": "typing", "show": False}))
                await websocket.send_text(json.dumps({"type": "message", "text": bot_reply}))
                session["messages"].append({"role": "bot", "text": bot_reply})

                # Статус — ожидание врача
                await websocket.send_text(json.dumps({
                    "type": "status_update",
                    "text": "⏳ Ожидает ответа врача",
                    "system_msg": "ℹ️ Вся информация передана доктору. Ожидайте — врач ответит вам в ближайшее время."
                }))

                session["status"] = "waiting_doctor"
                soap = await generate_soap(session_id)
                session["soap"] = soap

                # Уведомить кокпит врача
                await notify_cockpit({
                    "type": "new_case",
                    "session_id": session_id,
                    "preview": user_text[:60],
                    "soap": soap,
                    "time": datetime.now().strftime("%H:%M"),
                    "messages_count": len([m for m in session["messages"] if m["role"] == "parent"]),
                })
            else:
                bot_reply = await get_ai_response(session_id, user_text, phase)
                await websocket.send_text(json.dumps({"type": "typing", "show": False}))
                await websocket.send_text(json.dumps({"type": "message", "text": bot_reply}))
                session["messages"].append({"role": "bot", "text": bot_reply})

    except WebSocketDisconnect:
        parent_ws.pop(session_id, None)


# --------------------------------------------------------------------------
# ВРАЧ — кокпит
# --------------------------------------------------------------------------

COCKPIT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Doc Orchestra — Кокпит врача</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #f1f5f9; min-height: 100vh; }
.header { background: #1e3a5f; color: white; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 20px; } .header .badge { background: #ef4444; color: white; border-radius: 12px; padding: 2px 10px; font-size: 13px; }
.mode-badge { background: #f59e0b; color: #7c2d12; padding: 3px 10px; border-radius: 8px; font-size: 12px; font-weight: bold; }
.layout { display: flex; height: calc(100vh - 60px); }
.sidebar { width: 300px; background: white; border-right: 1px solid #e2e8f0; overflow-y: auto; }
.sidebar-header { padding: 16px; border-bottom: 1px solid #e2e8f0; font-weight: 600; color: #475569; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
.case-item { padding: 14px 16px; border-bottom: 1px solid #f1f5f9; cursor: pointer; transition: background 0.1s; }
.case-item:hover { background: #f8fafc; }
.case-item.active { background: #eff6ff; border-left: 3px solid #2563eb; }
.case-item.urgent { border-left: 3px solid #ef4444; }
.case-item .title { font-weight: 600; font-size: 14px; color: #1e293b; display: flex; align-items: center; gap: 6px; }
.case-item .preview { font-size: 12px; color: #64748b; margin-top: 4px; }
.case-item .meta { font-size: 11px; color: #94a3b8; margin-top: 4px; display: flex; justify-content: space-between; }
.main { flex: 1; padding: 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
.empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #94a3b8; gap: 12px; }
.empty .icon { font-size: 48px; }
.soap-card { background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.soap-card h2 { font-size: 16px; color: #1e293b; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
.soap-content { font-family: monospace; font-size: 13px; line-height: 1.8; color: #334155; white-space: pre-wrap; background: #f8fafc; padding: 16px; border-radius: 8px; }
.actions { background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.actions h3 { font-size: 14px; color: #475569; margin-bottom: 12px; }
.draft-edit { width: 100%; min-height: 80px; padding: 12px; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 14px; resize: vertical; margin-bottom: 12px; }
.btn-row { display: flex; gap: 10px; }
.btn { padding: 10px 24px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: opacity 0.15s; }
.btn:hover { opacity: 0.85; }
.btn-approve { background: #22c55e; color: white; }
.btn-edit { background: #f59e0b; color: white; }
.btn-reject { background: #ef4444; color: white; }
.toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b; color: white; padding: 12px 20px; border-radius: 8px; font-size: 14px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 100; }
.toast.show { opacity: 1; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-waiting { background: #f59e0b; animation: pulse 1.5s infinite; }
.dot-approved { background: #22c55e; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:16px">
    <h1>🎻 Doc Orchestra — Кокпит врача</h1>
    <span class="badge" id="count-badge" style="display:none">0</span>
  </div>
  <span class="mode-badge" id="mode-badge" style="background:#22c55e;color:white">MODE_PLACEHOLDER</span>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="sidebar-header">Очередь запросов</div>
    <div id="case-list">
      <div style="padding:24px;color:#94a3b8;font-size:13px;text-align:center">
        Ожидаем запросы от родителей...<br><br>
        <a href="/parent" target="_blank" style="color:#2563eb">↗ Открыть интерфейс родителя</a>
      </div>
    </div>
  </div>
  <div class="main" id="main-content">
    <div class="empty">
      <div class="icon">📋</div>
      <div style="font-size:16px;font-weight:500">Очередь пуста</div>
      <div style="font-size:13px">Новые запросы появятся здесь автоматически</div>
      <a href="/parent" target="_blank" style="color:#2563eb;font-size:13px;margin-top:8px">Открыть интерфейс родителя для теста →</a>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let ws;
let cases = {};
let activeSession = null;

function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws/cockpit');
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'new_case') handleNewCase(data);
    else if (data.type === 'init') { data.cases.forEach(c => handleNewCase(c)); }
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

function handleNewCase(data) {
  cases[data.session_id] = data;
  renderSidebar();
  if (!activeSession) selectCase(data.session_id);
  showToast('🔔 Новый запрос от родителя');
}

function renderSidebar() {
  const list = document.getElementById('case-list');
  const count = Object.keys(cases).length;
  const badge = document.getElementById('count-badge');
  badge.textContent = count;
  badge.style.display = count > 0 ? 'inline' : 'none';

  if (count === 0) {
    list.innerHTML = '<div style="padding:24px;color:#94a3b8;font-size:13px;text-align:center">Нет запросов</div>';
    return;
  }

  list.innerHTML = Object.values(cases).map(c => `
    <div class="case-item ${c.session_id === activeSession ? 'active' : ''}" onclick="selectCase('${c.session_id}')">
      <div class="title">
        <span class="status-dot ${c.status === 'approved' ? 'dot-approved' : 'dot-waiting'}"></span>
        Пациент · ${c.session_id}
      </div>
      <div class="preview">${c.preview || 'Нет описания'}</div>
      <div class="meta">
        <span>${c.time}</span>
        <span>${c.status === 'approved' ? '✅ Одобрено' : '⏳ Ожидает'}</span>
      </div>
    </div>
  `).join('');
}

function selectCase(sessionId) {
  activeSession = sessionId;
  const c = cases[sessionId];
  renderSidebar();

  const draft = extractDraft(c.soap || '');

  document.getElementById('main-content').innerHTML = `
    <div class="soap-card">
      <h2>📋 SOAP Заметка
        <span style="font-size:12px;background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:6px">
          ${c.status === 'approved' ? '✅ Одобрено' : '⏳ Ожидает одобрения'}
        </span>
      </h2>
      <div class="soap-content">${(c.soap || 'Генерация...').replace(/[⚠️💬📋🔬💊]/g, m => m)}</div>
    </div>
    <div class="actions">
      <h3>💬 Ответ родителю (отредактируй при необходимости):</h3>
      <textarea class="draft-edit" id="draft-text">${draft}</textarea>
      <div class="btn-row">
        <button class="btn btn-approve" onclick="approveCase('${sessionId}')">✅ Одобрить и отправить</button>
        <button class="btn btn-edit" onclick="focusDraft()">✏️ Редактировать</button>
        <button class="btn btn-reject" onclick="rejectCase('${sessionId}')">✗ Отклонить</button>
      </div>
    </div>
  `;
}

function extractDraft(soap) {
  const match = soap.match(/💬[^:]*:([\s\S]*?)(?=$|\\n\\n)/);
  if (match) return match[1].trim().replace(/«|»/g, '');
  return 'Доктор ознакомился с вашим запросом. Пожалуйста, следуйте рекомендациям ниже...';
}

function focusDraft() { document.getElementById('draft-text')?.focus(); }

function approveCase(sessionId) {
  const draft = document.getElementById('draft-text')?.value || '';
  ws.send(JSON.stringify({ type: 'approve', session_id: sessionId, message: draft }));
  cases[sessionId].status = 'approved';
  renderSidebar();
  selectCase(sessionId);
  showToast('✅ Ответ одобрен и отправлен родителю');
}

function rejectCase(sessionId) {
  const reason = prompt('Причина отклонения:');
  if (reason) {
    ws.send(JSON.stringify({ type: 'reject', session_id: sessionId, reason: reason }));
    showToast('✗ Кейс отклонён: ' + reason);
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

connect();
</script>
</body></html>"""


@app.get("/cockpit", response_class=HTMLResponse)
async def cockpit_page():
    mode = "⚠️ DEMO MODE" if DEMO_MODE else "✅ Claude AI подключён"
    return HTMLResponse(COCKPIT_HTML.replace("MODE_PLACEHOLDER", mode))


@app.websocket("/ws/cockpit")
async def cockpit_websocket(websocket: WebSocket):
    await websocket.accept()
    cockpit_ws.append(websocket)

    # Отправить существующие кейсы при подключении
    existing = [
        {"type": "new_case", "session_id": sid, **{k: v for k, v in s.items() if k != "messages"}}
        for sid, s in sessions.items()
        if s.get("status") in ("waiting_doctor", "approved")
    ]
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
                    sessions[sid]["approved_message"] = approved_msg
                    # Отправить ответ родителю
                    parent_socket = parent_ws.get(sid)
                    if parent_socket:
                        try:
                            await parent_socket.send_text(json.dumps({
                                "type": "status_update",
                                "text": "✅ Врач ответил",
                                "system_msg": f"👨‍⚕️ Ответ врача: {approved_msg}"
                            }))
                            await parent_socket.send_text(json.dumps({
                                "type": "message",
                                "text": f"👨‍⚕️ {approved_msg}"
                            }))
                        except Exception:
                            pass

            elif data.get("type") == "reject":
                sid = data["session_id"]
                if sid in sessions:
                    sessions[sid]["status"] = "rejected"

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
    print(f"  Режим: {'⚠️  DEMO (без Gemini)' if DEMO_MODE else '✅ REAL (Gemini подключён)'}")
    print()
    print("  Открой в браузере:")
    print("  📱 Родитель  →  http://localhost:8081/parent")
    print("  🩺 Врач      →  http://localhost:8081/cockpit")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
