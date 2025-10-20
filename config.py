import os
from dotenv import load_dotenv

load_dotenv(".env")



REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")


# получаем строку из env
allowed_users_str = os.getenv("ALLOWED_USERS", "")
# превращаем в список чисел
ALLOWED_USERS = [int(user_id.strip()) for user_id in allowed_users_str.split(",") if user_id.strip()]
