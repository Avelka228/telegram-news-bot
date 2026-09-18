import os
import requests
import hashlib
import re
import json
import time
import random
from bs4 import BeautifulSoup
from google import genai
from datetime import datetime

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
}

# --- Настройка Gemini ---
client = genai.Client(api_key=GOOGLE_API_KEY)
MODEL_ID = "gemini-3.5-flash-lite"

processed_file = "processed_posts.json"

def load_processed_hashes():
    if os.path.exists(processed_file):
        with open(processed_file, "r") as f:
            return set(json.load(f))
    return set()

def save_processed_hashes(hashes):
    with open(processed_file, "w") as f:
        json.dump(list(hashes), f)

def get_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def parse_channel(channel_key):
    """Парсит веб-версию канала и возвращает 5 последних постов с ссылками внутри текста."""
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
    
    # Берём только последние 5 постов
    for msg in message_divs[-5:]:
        text_el = msg.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        
        # Получаем чистый текст БЕЗ ссылок для дедупликации
        text_plain = text_el.get_text(" ", strip=True)
        
        # Извлекаем все ссылки из поста (кроме внутренних t.me)
        external_links = []
        for a in text_el.find_all("a", href=True):
            href = a["href"]
            if "t.me" not in href and "telegram.me" not in href:
                external_links.append({"url": href, "anchor": a.get_text(strip=True)})
        
        # Картинка
        img_el = msg.select_one(".tgme_widget_message_photo_wrap")
        img_url = None
        if img_el:
            style = img_el.get("style", "")
            match = re.search(r"url\('([^']+)'\)", style)
            if match:
                img_url = match.group(1)
        
        post_link_el = msg.select_one("a.tgme_widget_message_date")
        post_link = post_link_el["href"] if post_link_el else None
        
        if text_plain and post_link:
            posts.append({
                "text": text_plain,
                "external_links": external_links,
                "image_url": img_url,
                "link": post_link,
                "channel_name": CHANNELS[channel_key]["name"],
            })
    
    return posts

def get_all_new_posts():
    processed = load_processed_hashes()
    new_posts = []
    
    for channel_key in CHANNELS.keys():
        print(f"Парсим канал: {CHANNELS[channel_key]['name']}")
        posts = parse_channel(channel_key)
        print(f"  Найдено постов (последние 5): {len(posts)}")
        for post in posts:
            text_hash = get_hash(post["text"])
            if text_hash not in processed:
                post["hash"] = text_hash
                new_posts.append(post)
    
    return new_posts, processed

def process_with_ai(posts):
    """Отправляет посты в Gemini, чтобы выбрать одну лучшую новость."""
    if not posts:
        return None
    
    content_for_ai = ""
    for i, post in enumerate(posts):
        content_for_ai += f"--- ПОСТ {i+1} ---\n"
        content_for_ai += f"Канал: {post['channel_name']}\n"
        content_for_ai += f"Текст: {post['text'][:800]}\n"
        if post['external_links']:
            content_for_ai += "Внешние ссылки в посте:\n"
            for link in post['external_links']:
                content_for_ai += f"  - URL: {link['url']} (текст ссылки: '{link['anchor']}')\n"
        content_for_ai += f"Есть картинка: {'да' if post['image_url'] else 'нет'}\n\n"
    
    prompt = f"""
    Ты — редактор технологического Telegram-канала для русскоязычной аудитории. 
    Твой стиль — как у лучших каналов про гаджеты и технологии (Wylsacom, Rozetked, Big Geek):
    живой, увлекательный, но без кликбейта, КАПСА и жёлтых заголовков.

    Ниже список постов из нескольких Telegram-каналов.

    ЭТАП 1 — ОТБОР:
    Отбрось посты, которые НЕ подходят для публикации:
    - Личные посты: автор делится своим мнением, личными фото, личным видео, личным опытом использования.
    - Рекламные и спонсорские посты, промокоды, интеграции.
    - Опросы, мемы без новостной ценности, поздравления, посты "просто поболтать".
    
    ЭТАП 2 — ВЫБОР:
    Из оставшихся выбери РОВНО ОДИН самый интересный, свежий и значимый для аудитории.
    Правило: если один пост с картинкой, но скучный, а другой без картинки, но интересный — выбирай интересный.

    ЭТАП 3 — НАПИСАНИЕ ПОСТА:
    1. Объём: 3-5 абзацев, до 700 символов.
    2. Стиль: живо, увлекательно, как рассказывает друг. Без кликбейта и без "шок-контента".
    3. Оформление: <b>жирный</b> для ключевого акцента (1-2 раза максимум).
    4. ХЕШТЕГИ НЕ ДОБАВЛЯЙ ВООБЩЕ.
    5. НЕ пиши "источник: X", "подробнее на канале Y", "читайте на сайте Z". Мы сами источник информации.
    6. ЕСЛИ в исходном посте есть внешняя ссылка на первоисточник (Apple, Google, известный инсайдер, официальный блог) — можешь вставить её как HTML-гиперссылку ВНУТРИ слова.
       Пример: <a href="https://apple.com">Apple</a> представила новый чип M5.
       Если внешней ссылки на первоисточник нет — НЕ добавляй никаких ссылок.
    7. Никакого Markdown. Только HTML.

    Верни ответ строго в формате JSON:
    {{
      "post_text": "<b>Заголовок или первое предложение</b>\\n\\nОсновной текст поста без хештегов и без упоминания источника.",
      "post_index": номер_выбранного_поста_от_1_до_{len(posts)},
      "reason": "кратко почему выбрал именно этот пост"
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
    
    # Случайная задержка до 5 минут, чтобы посты выходили не ровно в :00
    delay = random.randint(0, 300)
    print(f"Случайная задержка: {delay} секунд")
    time.sleep(delay)
    
    new_posts, processed = get_all_new_posts()
    print(f"Найдено новых постов: {len(new_posts)}")
    
    if new_posts:
        result = process_with_ai(new_posts)
        if result and publish_to_telegram(result, new_posts):
            for post in new_posts:
                processed.add(post["hash"])
            save_processed_hashes(processed)
            print("История обновлена.")
    else:
        print("Новых постов нет, публикация не требуется.")
