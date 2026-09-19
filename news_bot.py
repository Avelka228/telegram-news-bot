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

# Минимальный интервал между публикациями (в минутах)
MIN_INTERVAL_MINUTES = 30

# Сколько дней хранить историю публикаций
HISTORY_DAYS = 3


def load_data():
    """Загружает хеши постов, хеши картинок, историю публикаций."""
    if os.path.exists(processed_file):
        with open(processed_file, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                return set(), set(), [], None
        
        # Поддержка старого формата (просто список хешей)
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
    """Сохраняет всё состояние в файл."""
    # Чистим старые записи (старше HISTORY_DAYS дней)
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
            # Если дата некорректная — оставляем запись на всякий случай
            cleaned_history.append(item)
    
    # Чистим хеши картинок, которых уже нет в истории
    active_image_hashes = set()
    for item in cleaned_history:
        if "image_hash" in item:
            active_image_hashes.add(item["image_hash"])
    
    # Оставляем только те хеши картинок, что в активной истории
    # (но также сохраняем те, что не привязаны к истории — на случай старых записей)
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
    """Проверяет, не было ли публикации за последние MIN_INTERVAL_MINUTES минут."""
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
    """Парсит веб-версию канала и возвращает 5 последних постов."""
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
    """Собирает последние посты со всех каналов, отсеивает опубликованные."""
    published, image_hashes, history, last_publish = load_data()
    candidates = []
    skipped_by_image = 0
    
    for channel_key in CHANNELS.keys():
        print(f"Парсим канал: {CHANNELS[channel_key]['name']}")
        posts = parse_channel(channel_key)
        print(f"  Найдено постов (последние 5): {len(posts)}")
        for post in posts:
            text_hash = get_hash(post["text"])
            
            # Пропускаем по тексту
            if text_hash in published:
                continue
            
            # Пропускаем по картинке (если та же картинка уже была)
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
    """Отправляет посты в Gemini, чтобы выбрать одну лучшую новость."""
    if not posts:
        return None
    
    content_for_ai = ""
    for i, post in enumerate(posts):
        content_for_ai += f"--- ПОСТ {i+1} (свежесть: {post.get('date', 'неизвестно')}) ---\n"
        content_for_ai += f"Канал: {post['channel_name']}\n"
        content_for_ai += f"Текст: {post['text'][:800]}\n"
        if post['external_links']:
            content_for_ai += "Внешние ссылки в посте:\n"
            for link in post['external_links']:
                content_for_ai += f"  - URL: {link['url']} (текст: '{link['anchor']}')\n"
        content_for_ai += f"Есть картинка: {'да' if post['image_url'] else 'НЕТ'}\n\n"
    
    # Формируем список недавних публикаций для проверки на дубли
    recent_published = ""
    if history:
        recent_published = "УЖЕ ОПУБЛИКОВАНО ЗА ПОСЛЕДНИЕ ДНИ (НЕ БРАТЬ ПОВТОРЫ):\n"
        for item in history[-30:]:
            recent_published += f"- {item.get('summary', '')}\n"
        recent_published += "\n"
    
    prompt = f"""
    Ты — редактор топового технологического Telegram-канала для русскоязычной аудитории.
    Твой стиль — как у Wylsacom и Rozetked: живо, по делу, с лёгкой иронией, но без кликбейта и КАПСА.

    Ниже список постов из нескольких Telegram-каналов. Посты УЖЕ отсортированы по свежести: 
    Пост 1 — самый свежий, последний — самый старый.

    {recent_published}

    ЭТАП 1 — ОТБОР (отбрось всё, что не подходит):
    - Личные посты: автор делится своим мнением, личными фото/видео, личным опытом.
    - Реклама, спонсорские посты, промокоды, интеграции.
    - Опросы, мемы без новостной ценности, поздравления, "просто поболтать".
    - Посты без новостной ценности (например, "смотрите какой закат").
    - НОВОСТИ, КОТОРЫЕ УЖЕ ЕСТЬ В СПИСКЕ "УЖЕ ОПУБЛИКОВАНО" ВЫШЕ. Если новость похожа по смыслу — пропусти!

    ЭТАП 2 — ВЫБОР (очень важно!):
    Из оставшихся выбери РОВНО ОДИН пост.
    ПРИОРИТЕТ №1: СВЕЖЕСТЬ. Отдавай предпочтение самым новым постам.
    ПРИОРИТЕТ №2: ПОСТЫ С КАРТИНКОЙ. Пост без картинки бери ТОЛЬКО если он реально топовый.
    Приоритет №3: Интересность для аудитории (гаджеты, IT, технологии).

    ЭТАП 3 — НАПИСАНИЕ ПОСТА:

    СТРУКТУРА (обязательно соблюдай!):
    - Абзац 1: КОРОТКИЙ цепляющий хук, до 60 символов, выделен <b>жирным</b>.
    - Пустая строка между всеми абзацами!
    - Абзац 2: суть новости, 1-2 предложения.
    - Пустая строка!
    - Абзац 3: детали/подробности, 2-3 предложения.
    - Пустая строка!
    - Абзац 4 (опционально): вывод или ирония.

    В Telegram перенос строки делается через \\n\\n — ДВА символа переноса между абзацами. Обязательно!

    ЗАПРЕЩЕНО:
    - Хештеги — НИКОГДА.
    - Упоминания "подробнее на канале X", "читайте на сайте Y", "источник: Z". Мы сами источник!
    - Markdown (звёздочки, подчёркивания). Только HTML.
    - Слипшийся текст без абзацев.

    РАЗРЕШЕНО:
    - <b>жирный</b> — 1-3 раза для акцентов.
    - Гиперссылка <a href="URL">текст</a> — ТОЛЬКО если в оригинальном посте есть ссылка на первоисточник (Apple, Google, известный инсайдер).
      Пример: <a href="https://apple.com">Apple</a> представила новый чип M5.
      Если внешней ссылки в посте нет — НЕ добавляй никаких ссылок.

    ОБЪЁМ: 400-700 символов.

    Верни ответ строго в формате JSON:
    {{
      "post_text": "<b>Короткий цепляющий заголовок</b>\\n\\nПервый абзац.\\n\\nВторой абзац.\\n\\nТретий абзац.",
      "post_index": номер_выбранного_поста_от_1_до_{len(posts)},
      "summary": "краткое описание новости в 10-15 слов (для проверки на дубли в будущем)",
      "reason": "кратко почему выбрал именно этот пост"
    }}

    Если ВСЕ посты являются дублями уже опубликованных — верни:
    {{
      "post_index": 0,
      "reason": "все посты — дубли"
    }}

    Вот посты (от самого свежего к самому старому):
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
    """Публикует пост с картинкой (или без) в Telegram-канал."""
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
    
    # Проверяем интервал
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
            
            # Если AI вернул 0 — все дубли
            if not result or result.get("post_index", 0) == 0:
                print("AI не нашёл новых интересных новостей (все дубли). Пропускаем.")
            else:
                post_index = result.get("post_index", 1) - 1
                if 0 <= post_index < len(candidates):
                    chosen = candidates[post_index]
                    if publish_to_telegram(result, candidates):
                        # Сохраняем хеш текста
                        published.add(chosen["hash"])
                        
                        # Сохраняем хеш картинки
                        if chosen.get("image_hash"):
                            image_hashes.add(chosen["image_hash"])
                        
                        # Добавляем запись в историю
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
