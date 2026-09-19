import os
import requests
import hashlib
import re
import json
import time
import random
from bs4 import BeautifulSoup
from google import genai
from datetime import datetime, timezone, timedelta

# --- Конфигурация ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

# --- Список Telegram-каналов ---
CHANNELS = {
    "DeepTechNET":   {"name": "DeepTech",     "url": "https://t.me/DeepTechNET"},
    "Wylsared":      {"name": "Wylsacom",     "url": "https://t.me/Wylsared"},
    "Romancev768":   {"name": "Romancev768",  "url": "https://t.me/Romancev768"},
    "biggeekru":     {"name": "Big Geek",     "url": "https://t.me/biggeekru"},
    "iphonesru":     {"name": "iPhones.ru",   "url": "https://t.me/iphonesru"},
    "technomedia":   {"name": "Техночат",     "url": "https://t.me/technomedia"},
    "naebnet":       {"name": "NN",           "url": "https://t.me/naebnet"},
    "rozetked":      {"name": "Rozetked",     "url": "https://t.me/rozetked"},
    "typespace":     {"name": "Тайпспейс",    "url": "https://t.me/typespace"},
}

# --- Настройка Gemini ---
client = genai.Client(api_key=GOOGLE_API_KEY)
MODEL_ID = "gemini-3.5-flash-lite"

processed_file = "processed_posts.json"

MIN_INTERVAL_MINUTES = 90
HISTORY_DAYS = 3


def load_data():
    if os.path.exists(processed_file):
        with open(processed_file, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                return set(), set(), [], None
        if isinstance(data, list):
            return set(data), set(), [], None
        return (
            set(data.get("hashes", [])),
            set(data.get("image_hashes", [])),
            data.get("history", []),
            data.get("last_publish")
        )
    return set(), set(), [], None


def save_data(hashes, image_hashes, history, last_publish=None):
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    cleaned_history = []
    for item in history:
        try:
            item_date = datetime.fromisoformat(item.get("date", ""))
            if item_date.tzinfo is None:
                item_date = item_date.replace(tzinfo=timezone.utc)
            if item_date > cutoff:
                cleaned_history.append(item)
        except Exception:
            cleaned_history.append(item)
    
    active_image_hashes = set()
    for item in cleaned_history:
        if "image_hash" in item:
            active_image_hashes.add(item["image_hash"])
    
    final_image_hashes = image_hashes & active_image_hashes if active_image_hashes else image_hashes
    
    data = {
        "hashes": list(hashes),
        "image_hashes": list(final_image_hashes),
        "history": cleaned_history,
        "last_publish": last_publish
    }
    with open(processed_file, "w") as f:
        json.dump(data, f)


def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def should_skip_due_to_interval(last_publish_str):
    if not last_publish_str:
        return False
    try:
        last_publish = datetime.fromisoformat(last_publish_str)
        if last_publish.tzinfo is None:
            last_publish = last_publish.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff_minutes = (now - last_publish).total_seconds() / 60
        if diff_minutes < MIN_INTERVAL_MINUTES:
            print(f"⚠️ Прошло всего {diff_minutes:.1f} мин с прошлой публикации. Пропускаем.")
            return True
        return False
    except Exception as e:
        print(f"Ошибка при проверке времени: {e}")
        return False


def parse_channel(channel_key):
    url = f"https://t.me/s/{channel_key}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"Не удалось загрузить канал {channel_key}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    message_divs = soup.select("div.tgme_widget_message_wrap")
    
    for msg in message_divs[-5:]:
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text_plain = text_el.get_text(" ", strip=True)
        
        external_links = []
        for a in text_el.find_all("a", href=True):
            href = a["href"]
            if "t.me" not in href and "telegram.me" not in href:
                external_links.append({"url": href, "anchor": a.get_text(strip=True)})
        
        img_el = msg.select_one(".tgme_widget_message_photo_wrap")
        img_url = None
        if img_el:
            style = img_el.get("style", "")
            match = re.search(r"url\('([^']+)'\)", style)
            if match:
                img_url = match.group(1)
        
        post_link_el = msg.select_one("a.tgme_widget_message_date")
        post_link = post_link_el["href"] if post_link_el else None
        
        time_el = msg.select_one("time")
        post_date = time_el["datetime"] if time_el and time_el.has_attr("datetime") else ""
        
        if text_plain and post_link:
            posts.append({
                "text": text_plain,
                "external_links": external_links,
                "image_url": img_url,
                "image_hash": get_hash(img_url) if img_url else None,
                "link": post_link,
                "channel_name": CHANNELS[channel_key]["name"],
                "date": post_date,
            })
    return posts


def get_all_posts_for_ai():
    published, image_hashes, history, last_publish = load_data()
    candidates = []
    skipped_by_image = 0
    
    for channel_key in CHANNELS.keys():
        print(f"Парсим канал: {CHANNELS[channel_key]['name']}")
        posts = parse_channel(channel_key)
        print(f"  Найдено постов (последние 5): {len(posts)}")
        for post in posts:
            text_hash = get_hash(post["text"])
            if text_hash in published:
                continue
            if post.get("image_hash") and post["image_hash"] in image_hashes:
                skipped_by_image += 1
                print(f"  ⏭️ Пропускаем пост из {post['channel_name']} — картинка уже была")
                continue
            post["hash"] = text_hash
            candidates.append(post)
    
    if skipped_by_image:
        print(f"Всего пропущено по картинке: {skipped_by_image}")
    
    candidates.sort(key=lambda p: p.get("date", ""), reverse=True)
    return candidates, published, image_hashes, history, last_publish


def process_with_ai(posts, history):
    if not posts:
        return None
    
    content_for_ai = ""
    for i, post in enumerate(posts):
        content_for_ai += f"--- ПОСТ {i+1} (свежесть: {post.get('date', 'неизвестно')}) ---\n"
        content_for_ai += f"Канал: {post['channel_name']}\n"
        content_for_ai += f"Текст: {post['text'][:800]}\n"
        if post['external_links']:
            content_for_ai += "Внешние ссылки в посте (на первоисточники):\n"
            for link in post['external_links']:
                content_for_ai += f"  - URL: {link['url']} (текст ссылки: '{link['anchor']}')\n"
        else:
            content_for_ai += "Внешних ссылок нет.\n"
        content_for_ai += f"Есть картинка: {'да' if post['image_url'] else 'НЕТ'}\n\n"
    
    recent_published = ""
    if history:
        recent_published = "УЖЕ ОПУБЛИКОВАНО ЗА ПОСЛЕДНИЕ ДНИ (НЕ БРАТЬ ПОВТОРЫ):\n"
        for item in history[-30:]:
            recent_published += f"- {item.get('summary', '')}\n"
        recent_published += "\n"
    
    prompt = f"""
    Ты — редактор топового технологического Telegram-канала «Техзадача на вечер» для русскоязычной аудитории.
    Твой стиль — как у Wylsacom, Rozetked, Big Geek: живо, по делу, с юмором, но без кликбейта и КАПСА.

    Твоя цель: писать так, чтобы человек захотел прочитать пост до конца и поставить реакцию.

    Ниже список постов из нескольких Telegram-каналов. Посты УЖЕ отсортированы по свежести.

    {recent_published}

    ═══════════════════════════════════════════════════
    ЭТАП 1 — ОТБРОСИТЬ МУСОР:
    ═══════════════════════════════════════════════════
    ❌ Реклама, промокоды, партнёрские посты, розыгрыши
    ❌ Личное: мнение автора, личные фото, поздравления, опросы
    ❌ Дубли из списка "УЖЕ ОПУБЛИКОВАНО"

    ═══════════════════════════════════════════════════
    ЭТАП 2 — ВЫБРАТЬ ЛУЧШЕЕ:
    ═══════════════════════════════════════════════════
    🥇 Топ: новые устройства, крупные анонсы, скандалы, прорывы, сделки, утечки флагманов
    🥈 Средние: обзоры, важные обновления сервисов
    🥉 Мелкие: берём, если нет топовых

    ⚠️ Пропуск (post_index: 0) — ТОЛЬКО если ВСЁ реклама/личное/дубли или пусто.

    ═══════════════════════════════════════════════════
    ЭТАП 3 — НАПИСАНИЕ ПОСТА. ЧИТАЙ ВНИМАТЕЛЬНО!
    ═══════════════════════════════════════════════════

    📐 СТРУКТУРА:
    - Строка 1: Telegram Hook. Короткая цепляющая фраза до 60 символов, <b>жирным</b>.
    - Пустая строка (\\n\\n)
    - Абзац 1: суть новости, 1-2 предложения.
    - Пустая строка (\\n\\n)
    - Абзац 2: детали/подробности, 2-3 предложения.
    - Пустая строка (\\n\\n)
    - Абзац 3 (опционально): вывод, ирония, комментарий от автора.

    ❌ НЕ делать разделителей между абзацами (никаких ━━━, ▪ ▪ ▪, · · ·, ———).
    Просто пустая строка — и всё.

    🎨 ЭМОДЗИ В НАЧАЛЕ ЗАГОЛОВКА — ВАЖНО:
    
    ❌ НЕ используй одну и ту же эмодзи каждый раз!
    ❌ НЕ ставь ⚡ или ✨ в начале каждого поста! Это выглядит как робот.
    
    ✅ Каждый раз выбирай эмодзи ПО СМЫСЛУ новости. Варьируй! Человек так делает.
    
    Подсказки по эмодзи (используй разные):
    - Гаджеты/устройства: 📱 📟 🖥️ ⌨️ 🎧 📷 🎮
    - Софт/приложения: 📲 💾 🛠️ ⚙️ 🔧
    - Компании/бизнес: 📰 🏢 💼 📊
    - Финансы/сделки: 💰 💸 📈 📉
    - ИИ/технологии: 🤖 🧠 💡 🚀
    - Скандалы/суды: ⚠️ 🚨 ⚖️ 🔥
    - Наука/прорывы: 🔬 🧪 ⚛️ 🌌
    - Apple: 🍎
    - Android/Google: 🤖
    - Samsung/Galaxy: 🌌
    - Игры: 🎮 🕹️
    - Кибербезопасность: 🔐 🛡️ 🔒
    - Электро/авто: 🚗 ⚡ 🔋
    
    Иногда можно вообще без эмодзи в начале — просто жирный заголовок. 
    Так тоже бывает у топовых каналов! Разнообразь.

    📝 ЗАГОЛОВОК (Hook) — как писать:
    ✅ Хорошо:
    - "Apple показала iPhone 18 Pro — и это сюрприз"
    - "Samsung продала 10 млн Galaxy S26 за 3 дня"
    - "Илон Маск снова удивил — теперь нейросетью"
    - "Главная утечка недели: что покажет Google"
    - "Тим Кук в ярости из-за утечки"
    - "Новый чип M5 обходит конкурентов на 40%"
    
    ❌ Плохо:
    - "Не может быть! Apple показала iPhone" (кликбейт)
    - "Шок-контент!" (жёлтый стиль)
    - "Знаете ли вы, что..." (затянутый вопрос)
    - "Это изменит всё!" (пустые обещания)

    🎭 КОНКРЕТИКА И ФАКТЫ:
    ✅ "заряжается за 55 минут", "на 40% быстрее", "10 млн копий за 3 дня"
    ❌ "заряжается быстрее", "работает шустрее", "много копий продали"

    😄 ЮМОР И ИРОНИЯ — ИНОГДА (не в каждом посте, 30-40%):
    - "Правда, работает только в США. Как всегда."
    - "Что ж, ждём в следующем году."
    - "Инновация года, без сарказма."
    - "Apple снова изобрела то, что было у Android 3 года назад."

    💬 ЦИТАТА — ЕСЛИ ЕСТЬ В ИСХОДНОМ ПОСТЕ:
    Используй Telegram-формат цитаты через > в начале строки:
    > Это цитата кого-то важного
    
    Например:
    "> Мы изменили всё — Тим Кук.
    И действительно, iPhone стал другим."

    🔗 ССЫЛКИ НА ПЕРВОИСТОЧНИК — САМОЕ ГЛАВНОЕ ПРАВИЛО:

    ❌ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
    - "Источник: https://..."
    - "Ссылка: https://..."
    - "Подробнее: <a href='...'>...</a>" в конце поста
    - Отдельная строка со ссылкой в конце
    - Любая ссылка вне контекста

    ✅ ПРАВИЛЬНО — ссылка ВНУТРИ текста на осмысленном слове:
    "Apple показала новый чип M5. В <a href='https://apple.com/newsroom'>официальном анонсе</a> компания обещает 40% прирост."
    "Samsung <a href='https://samsung.com/news'>подтвердила</a> дату презентации."
    
    - Ссылку вставляй ТОЛЬКО из блока "Внешние ссылки в посте". НЕ выдумывай URL!
    - Если внешних ссылок нет — не вставляй вообще.

    🎁 ФИРМЕННАЯ КОНЦОВКА — РАЗ В 5 ПОСТОВ (не чаще!):
    Иногда в самом конце добавляй одну из этих фраз (чередуй, не повторяйся):
    - "— Техзадача на вечер"
    - "Больше новостей — в закрепе 👆"
    - "Ставь 🔥, если было интересно"
    - "А что думаешь? Пиши в комментах 👇"
    - "Ставь ❤️, если зашло"
    - "Реакция 🤯, если удивлён"
    - "Есть мысли? Ждём в комментариях"
    
    ⚠️ Только в 1 из 5 постов! НЕ в каждом! Иначе выглядит как робот.

    🎨 ОФОРМЛЕНИЕ:
    - <b>жирный</b> — 1-3 раза (заголовок + ключевые слова)
    - <i>курсив</i> — 0-2 раза, для иронии
    - Дополнительные эмодзи в тексте — редко, максимум 1-2 за пост
    - Ты можешь использовать смайлики которые пользователи могут ставить как реакции: 🔥 ❤️ 👀 💩 🤯 — но ТОЛЬКО в конце, если просишь реакцию

    ❌ ЗАПРЕЩЕНО:
    - Хештеги
    - "Подробнее на канале X", "читайте на сайте Y"
    - Markdown (звёздочки). Только HTML.
    - Слипшийся текст без абзацев
    - Ссылка в конце поста
    - Улыбочки 🙂😊😂 и мультяшные эмодзи (кроме реакций 🔥❤️👀💩🤯 в конце, если просишь реакцию)
    - Разделители между абзацами
    - Одна и та же эмодзи в начале каждого поста
    - Вопросы-интриги в стиле "Не может быть?"

    ОБЪЁМ: 400-700 символов.

    ═══════════════════════════════════════════════════
    ФОРМАТ ОТВЕТА (строго JSON):
    ═══════════════════════════════════════════════════
    {{
      "post_text": "🎯 <b>Заголовок с конкретикой</b>\\n\\nПервый абзац с сутью.\\n\\nВторой абзац с деталями и <a href='URL'>ссылкой внутри</a>.\\n\\nТретий абзац с иронией или выводом.",
      "post_index": номер_от_1_до_{len(posts)},
      "summary": "краткое описание новости в 10-15 слов",
      "reason": "почему выбрал именно этот пост"
    }}

    ТОЛЬКО если ВСЁ — мусор, верни:
    {{
      "post_index": 0,
      "reason": "нет реальных новостей"
    }}

    Вот посты:
    {content_for_ai}
    """
    
    response = None
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        result = json.loads(text)
        print(f"AI выбрал пост #{result.get('post_index')}: {result.get('reason', '')}")
        return result
    except Exception as e:
        print(f"Ошибка при обращении к Gemini: {e}")
        print(f"Сырой ответ: {response.text if response is not None else 'нет'}")
        return None


def publish_to_telegram(post_data, all_posts):
    if not post_data or not post_data.get("post_text"):
        print("Нет данных для публикации.")
        return False

    text = post_data["post_text"]
    post_index = post_data.get("post_index", 1) - 1
    
    if 0 <= post_index < len(all_posts):
        chosen_post = all_posts[post_index]
        image_url = chosen_post.get("image_url")
        print(f"Выбранный пост: {chosen_post['channel_name']}, картинка: {'есть' if image_url else 'нет'}")
    else:
        print(f"Индекс {post_index} вне диапазона")
        image_url = None
    
    response = None
    try:
        if image_url:
            print(f"Скачиваем картинку: {image_url}")
            img_response = requests.get(image_url, timeout=30)
            img_response.raise_for_status()
            
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            files = {"photo": ("image.jpg", img_response.content)}
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": text,
                "parse_mode": "HTML",
            }
            response = requests.post(url, data=data, files=files, timeout=60)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            response = requests.post(url, json=payload, timeout=30)
        
        response.raise_for_status()
        print("Сообщение успешно опубликовано!")
        return True
    except Exception as e:
        print(f"Ошибка при публикации в Telegram: {e}")
        print(f"Ответ: {response.text if response is not None else 'нет ответа'}")
        return False


if __name__ == "__main__":
    print(f"Запуск бота: {datetime.now()}")
    
    delay = random.randint(0, 300)
    print(f"Случайная задержка: {delay} секунд")
    time.sleep(delay)
    
    _, _, _, last_publish = load_data()
    if should_skip_due_to_interval(last_publish):
        print("Пропускаем запуск из-за недавней публикации.")
    else:
        candidates, published, image_hashes, history, _ = get_all_posts_for_ai()
        print(f"Найдено кандидатов (неопубликованных): {len(candidates)}")
        
        if not candidates:
            print("Все посты из последних 5 в каждом канале уже опубликованы. Пропускаем.")
        else:
            result = process_with_ai(candidates, history)
            
            if not result or result.get("post_index", 0) == 0:
                print("AI не нашёл реальных новостей. Пропускаем.")
            else:
                post_index = result.get("post_index", 1) - 1
                if 0 <= post_index < len(candidates):
                    chosen = candidates[post_index]
                    if publish_to_telegram(result, candidates):
                        published.add(chosen["hash"])
                        
                        if chosen.get("image_hash"):
                            image_hashes.add(chosen["image_hash"])
                        
                        now_utc = datetime.now(timezone.utc).isoformat()
                        history.append({
                            "date": now_utc,
                            "summary": result.get("summary", "")[:200],
                            "image_hash": chosen.get("image_hash"),
                            "channel": chosen["channel_name"]
                        })
                        
                        save_data(published, image_hashes, history, now_utc)
                        print(f"Опубликовано из канала: {chosen['channel_name']}")
                        print(f"История обновлена. Время: {now_utc}")
