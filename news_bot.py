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
            content_for_ai += "Внешние ссылки в посте:\n"
            for link in post['external_links']:
                content_for_ai += f"  - URL: {link['url']} (текст: '{link['anchor']}')\n"
        content_for_ai += f"Есть картинка: {'да' if post['image_url'] else 'НЕТ'}\n\n"
    
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

    ═══════════════════════════════════════════════════
    ЭТАП 1 — ОТБРОСИТЬ МУСОР (это точно НЕ публикуем):
    ═══════════════════════════════════════════════════

    ❌ РЕКЛАМА И ПРОМО (отбрасываем ВСЕГДА):
    - Промокоды, скидки, "купи по ссылке", "используй код"
    - Партнёрские материалы, интеграции, спонсорские посты
    - "Розыгрыш", "конкурс", "подпишись и получи"
    - Реферальные ссылки, реклама магазинов и сервисов
    - Посты с явной коммерческой целью

    ❌ ЛИЧНОЕ (отбрасываем):
    - Мнение автора, личный опыт, "я попробовал"
    - Личные фото/видео автора, "смотрите какой закат"
    - Поздравления, "с добрым утром", мемы без смысла
    - Опросы, "а вы как думаете?"

    ❌ ДУБЛИ (отбрасываем):
    - Всё, что похоже по смыслу на список "УЖЕ ОПУБЛИКОВАНО" выше

    ═══════════════════════════════════════════════════
    ЭТАП 2 — ВЫБРАТЬ ЛУЧШЕЕ ИЗ ОСТАВШЕГОСЯ:
    ═══════════════════════════════════════════════════

    После отброса мусора у тебя остались нормальные новости. 
    Оцени их и выбери САМУЮ ИНТЕРЕСНУЮ по такой шкале:

    🥇 ТОП-НОВОСТИ (приоритет):
    - Новые продукты и устройства (Samsung Galaxy S26, iPhone 18)
    - Крупные анонсы и утечки флагманов
    - Скандалы, суды, блокировки в IT
    - Прорывы в науке и технологиях (новые чипы, ИИ, квантовые компьютеры)
    - Крупные сделки, поглощения
    - Уход/приход топ-менеджеров крупных компаний

    🥈 СРЕДНИЕ:
    - Обзоры новых устройств
    - Важные обновления популярных сервисов
    - Интересные исследования и статистика

    🥉 МЕЛКИЕ (берём, если нет топовых и средних):
    - Мелкие обновления приложений
    - Второстепенные функции
    - Любая другая реальная новость

    ⚠️ ВАЖНО: Мелочь — это ВСЁ РАВНО НОВОСТЬ. Лучше опубликовать её, чем ничего.
    НЕ пропускай запуск, если есть хоть одна реальная новость. 

    Пропустить запуск (post_index: 0) можно ТОЛЬКО если:
    - ВСЕ посты — реклама/промо, ИЛИ
    - ВСЕ посты — личные/мемы/опросы, ИЛИ
    - ВСЕ посты — дубли уже опубликованного, ИЛИ
    - Список постов вообще пуст.

    Если осталась ХОТЬ ОДНА реальная новость — бери её. Даже если она мелкая.

    ПРИОРИТЕТЫ ПРИ ВЫБОРЕ:
    1. Значимость (топ > среднее > мелочь)
    2. Свежесть
    3. Наличие картинки

    ═══════════════════════════════════════════════════
    ЭТАП 3 — НАПИСАНИЕ ПОСТА:
    ═══════════════════════════════════════════════════
    СТРУКТУРА (обязательно):
    - Абзац 1: КОРОТКИЙ цепляющий хук, до 60 символов, выделен <b>жирным</b>.
    - Пустая строка между всеми абзацами!
    - Абзац 2: суть новости, 1-2 предложения.
    - Пустая строка!
    - Абзац 3: детали/подробности, 2-3 предложения.
    - Пустая строка!
    - Абзац 4 (опционально): вывод или ирония.

    В Telegram перенос строки делается через \\n\\n — ДВА символа переноса.

    ЗАПРЕЩЕНО:
    - Хештеги.
    - Упоминания "подробнее на канале X", "читайте на сайте Y", "источник: Z".
    - Markdown. Только HTML.
    - Слипшийся текст без абзацев.

    РАЗРЕШЕНО:
    - <b>жирный</b> — 1-3 раза.
    - Гиперссылка <a href="URL">текст</a> — ТОЛЬКО если в оригинальном посте есть ссылка на первоисточник.

    ОБЪЁМ: 400-700 символов.

    ═══════════════════════════════════════════════════
    ФОРМАТ ОТВЕТА (строго JSON):
    ═══════════════════════════════════════════════════
    {{
      "post_text": "<b>Заголовок</b>\\n\\nАбзац 1.\\n\\nАбзац 2.\\n\\nАбзац 3.",
      "post_index": номер_от_1_до_{len(posts)},
      "summary": "краткое описание новости в 10-15 слов",
      "reason": "почему выбрал именно этот пост"
    }}

    ТОЛЬКО если ВСЁ — мусор (реклама/личное/дубли), верни:
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
                print("AI не нашёл реальных новостей (только реклама/личное/дубли). Пропускаем.")
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
