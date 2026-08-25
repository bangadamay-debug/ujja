import os

# Bot token from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Admin Telegram user IDs
ADMIN_IDS = [1446058092, 6894923643]

# Required channels (username without @)
REQUIRED_CHANNELS = ["TheTricksMaster"]
REQUIRED_CHANNEL_LINKS = [
    "https://t.me/TheTricksMaster",
    "https://t.me/+m7ORE3sz5aFiNzJl",
]

# File paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACK_IMAGE_PATH = os.path.join(BASE_DIR, "ujala_pack.jpg")
DB_PATH = os.path.join(BASE_DIR, "bot_data.db")

# Points config
POINTS_PER_REFERRAL = 5   # points awarded to referrer per new user

# Ujala API
BASE_URL = "https://www.ujalahappiestonam.com"
API_BASE = f"{BASE_URL}/api"
BARCODE = "8902102126232"

# Retry config — increased for better reliability with 100 users
MAX_RETRIES = 5      # was 3
RETRY_DELAY = 3      # was 2 (seconds between retries)

# Thread pool: limits concurrent Ujala API calls so 100 users don't
# spawn hundreds of threads and exhaust OS resources.
MAX_WORKERS = 15

HEADERS_BASE = {
    "accept": "application/json",
    "origin": BASE_URL,
    "referer": f"{BASE_URL}/",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 15; Pixel 9) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Mobile Safari/537.36"
    ),
    "sec-ch-ua": '"Not A;Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

FIRST_NAMES = [
    "Rahul", "Amit", "Sanjay", "Vivek", "Arjun",
    "Priya", "Anjali", "Neha", "Pooja", "Sakshi",
    "Deepak", "Rajesh", "Manoj", "Suresh", "Anil",
]
SURNAMES = [
    "Nair", "Menon", "Pillai", "Kurian", "Varma",
    "Sharma", "Kumar", "Singh", "Patel", "Reddy",
]
CITIES = [
    "thiruvananthapuram", "kochi", "kozhikode",
    "thrissur", "kollam", "alappuzha", "palakkad",
]
