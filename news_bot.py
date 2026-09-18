import os
import feedparser
import requests
import google.generativeai as genai
from datetime import datetime

# --- Конфигурация ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

# Список RSS-лент. Замените на свои любимые источники!
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://habr.com/ru/rss/news/all/",
]

# --- Настройка Gemini ---
genai.configure(api_key=GOOGLE_API_KEY)
# Используем актуальную модель
model = genai.GenerativeModel('gemini-2.5-flash')

def get_news_from_rss():
    """Собирает заголовки и ссылки из RSS-лент."""
    all_news = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                all_news.append(f"- {entry.title}\n  {entry.link}")
        except Exception as e:
            print(f"Ошибка при чтении {url}: {e}")
    return "\n".join(all_news)

def process_with_ai(news_content):
    """Отправляет новости в Gemini для обработки."""
    prompt = f"""
    Ты — редактор новостного Telegram-канала на русском языке.
    Проанализируй список новостей ниже.
    Выбери 3-5 самых интересных и важных.
    Для каждой выбранной новости:
    1. Перефразируй её своими словами в одном абзаце (не копируй текст дословно).
    2. Добавь в конце ссылку на источник.
    Игнорируй рекламу и маркетинговые материалы.
    Верни готовый текст для публикации в Telegram на русском языке.
    Разделяй новости пустой строкой.

    Новости:
    {news_content}
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Ошибка при обращении к Gemini: {e}")
        return None

def publish_to_telegram(text):
    """Публикует готовый текст в Telegram-канал."""
    if not text:
        print("Нет текста для публикации.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        print("Сообщение успешно опубликовано!")
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при публикации в Telegram: {e}")
        print(f"Ответ сервера: {response.text if 'response' in locals() else 'нет ответа'}")

if __name__ == "__main__":
    print(f"Запуск бота: {datetime.now()}")
    raw_news = get_news_from_rss()
    if raw_news:
        processed_text = process_with_ai(raw_news)
        publish_to_telegram(processed_text)
    else:
        print("Не удалось собрать новости.")
