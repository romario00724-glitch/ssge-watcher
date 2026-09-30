#!/usr/bin/env python3
"""
ss.ge -> Telegram: нові оголошення від власників.

Команди:
  python watcher.py             перевіряти безперервно (кожні LOOP_MINUTES хв)
  python watcher.py --once      одна перевірка (для GitHub Actions / cron)
  python watcher.py --test      показати 3 свіжі оголошення й надіслати одне тестове в Telegram
  python watcher.py --chat-id   дізнатися chat_id (спершу напишіть боту /start)
  python watcher.py --reset     забути базу й наступного разу заново «запам'ятати» поточні оголошення
  python watcher.py --dump      зберегти «сирі» відповіді сайту у dump_list.json і dump_detail.json

Посилання на пошук беруться з searches.txt, база побачених оголошень — state.json.
"""

import argparse
import hashlib
import hmac
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─────────────────────────────── НАЛАШТУВАННЯ ───────────────────────────────
# Токен і chat_id беруться зі змінних середовища (на GitHub — Secrets),
# а на своєму комп'ютері — з файлу telegram.json поруч зі скриптом.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # кілька чатів — через кому
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # якщо задано — грузинські/англійські описи в чаті перекладаються
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5-mini"

LANG = "ru"                    # мова назв, описів і посилань з ss.ge: ru / en / ka
ONLY_OWNERS = True             # завжди додавати фільтр «От собственника» (individualEntityOnly)
PAGES_PER_RUN = 3              # скільки сторінок видачі перевіряти за один запуск
SEED_PAGES = 10                # скільки сторінок «запам'ятати» при першому запуску пошуку
PAGE_SIZE = 16                 # оголошень на сторінці — стільки ж, скільки показує сам сайт
FETCH_DETAILS = True           # догружати картку нового оголошення (телефон, автор)
AGENT_THRESHOLD = 3            # від скількох оголошень в одного автора ставити позначку ⚠️
SKIP_SUSPECTED_AGENTS = False  # True — такі оголошення взагалі не надсилати
MAX_ALERTS_PER_RUN = 25        # запобіжник від спаму; решта прийде наступного запуску
LOOP_MINUTES = 10              # інтервал у безперервному режимі
FAIL_ALERT_AFTER = 6           # після скількох невдалих перевірок поспіль написати в Telegram
KEEP_DAYS = 60                 # скільки днів пам'ятати побачені оголошення
TRANSLATE_ALERTS = True        # з OPENAI_API_KEY: перекладати російською грузинські/англійські опис і адресу в чаті
TRANSLATE_BUDGET_SECONDS = 180 # не більше стільки секунд на переклади за одну перевірку (запобіжник для GitHub)
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
SEARCHES_FILE = BASE_DIR / "searches.txt"
STATE_FILE = BASE_DIR / "state.json"
TELEGRAM_FILE = BASE_DIR / "telegram.json"  # {"token": "...", "chat_id": "...", "openai_key": "..."} — не викладати на GitHub!

API_URL = "https://api-gateway.ss.ge/v1/RealEstate/LegendSearch"
SITE = "https://home.ss.ge"
LISTING_ROOTS = {"ru": "недвижимость", "en": "real-estate", "ka": "udzravi-qoneba"}
GEORGIA_TZ = timezone(timedelta(hours=4))  # у Грузії немає переходу на літній час
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

try:  # curl_cffi відправляє запит «як справжній Chrome» — так менше шансів натрапити на блок
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

def ssl_context():
    """Набір сертифікатів certifi: на Mac вбудований Python часто їх не бачить."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def load_telegram_file():
    """Доповнює токен, chat_id і ключ OpenAI з telegram.json, якщо їх не задано змінними середовища."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
    if not TELEGRAM_FILE.exists():
        return
    try:
        cfg = json.loads(TELEGRAM_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"telegram.json не читається ({e}) — перевірте лапки й коми", flush=True)
        return
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN or str(cfg.get("token") or "").strip()
    TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID or str(cfg.get("chat_id") or "").strip()
    OPENAI_API_KEY = OPENAI_API_KEY or str(cfg.get("openai_key") or "").strip()


load_telegram_file()


class FetchError(Exception):
    pass


class ConfigError(Exception):
    pass


def log(msg):
    print(f"[{datetime.now(GEORGIA_TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def today():
    return datetime.now(GEORGIA_TZ).strftime("%Y-%m-%d")


def clean(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def plain_text(value):
    """Опис з сайту буває з HTML: <br />, &quot; тощо — робимо звичайний текст."""
    text = re.sub(r"(?i)<br\s*/?>", " ", str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return clean(html.unescape(text))


def as_dict(value):
    return value if isinstance(value, dict) else {}


# ─────────────────────────────── ss.ge ───────────────────────────────

# Назви з адреси сторінки пошуку → коди API. Так їх записує сам сайт, трьома мовами.
REAL_ESTATE_TYPES = {5: ("bina", "Flat", "Квартира"), 4: ("kerdzo-saxli", "Private-House", "Дом"),
                     1: ("agaraki", "Summer-Cottage", "Дача"), 2: ("sastumro", "Hotel", "Гостиница"),
                     3: ("mitsis-nakveti", "Land", "Земельный-участок"),
                     6: ("komerciuli", "Commercial-Real-Estate", "Комерческая-площадь")}
DEAL_TYPES = {1: ("qiravdeba", "For-Rent", "Аренда"), 2: ("giravdeba", "Lease", "Ипотека"),
              3: ("qiravdeba-dghiurad", "Daily-Rent", "Аренда--за-день"), 4: ("iyideba", "For-Sale", "Продается")}
# Параметри адреси сторінки пошуку, які сайт передає в API під тими самими назвами.
INT_LIST_PARAMS = ("realEstateStatuses", "commercialTypes", "cityIdList", "subdistrictIds", "streetIds",
                   "offerType", "rooms", "bedroomsCount")
INT_PARAMS = ("municipalityId", "subwayStationDistance", "areaFrom", "areaTo", "currencyId", "priceType",
              "priceFrom", "priceTo")
JSON_PARAMS = ("advancedSearch", "statuses")


def _http(url, headers=None, payload=None):
    """GET, а з payload — POST із JSON. Повертає (код, текст)."""
    try:
        if cffi_requests:
            if payload is None:
                resp = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=30)
            else:
                resp = cffi_requests.post(url, json=payload, headers=headers, impersonate="chrome", timeout=30)
            return resp.status_code, resp.text
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise FetchError(f"мережева помилка: {e}") from e


def check_status(status):
    if status != 200:
        hint = " — схоже на захист від ботів" if status in (403, 429, 503) else ""
        raise FetchError(f"HTTP {status}{hint}")


def page_data(url):
    """Дані сторінки сайту: Next.js кладе їх у <script id="__NEXT_DATA__">."""
    status, body = _http(url, {"Accept-Language": LANG})
    check_status(status)
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    if not match:
        raise FetchError("на сторінці немає даних — можливо, це сторінка перевірки від Cloudflare")
    try:
        return as_dict(as_dict(json.loads(match.group(1)).get("props")).get("pageProps"))
    except ValueError:
        raise FetchError("дані сторінки не читаються — можливо, сайт змінився")


_api_token = ""


def api_token(refresh=False):
    """Гостьовий ключ до API ss.ge. Сайт кладе його в кожну сторінку, діє він годину."""
    global _api_token
    if refresh or not _api_token:
        _api_token = clean(page_data(f"{SITE}/{LANG}/").get("credentialsToken"))
        if not _api_token:
            raise FetchError("на сторінці ss.ge немає ключа до API — можливо, сайт змінився")
    return _api_token


def api_headers():
    return {
        "Authorization": f"Bearer {api_token()}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": LANG,  # без нього назви й описи приходять англійською
        "lang": LANG,
        "Origin": SITE,
        "Referer": SITE + "/",
    }


def api_search(payload):
    for attempt in range(2):
        status, body = _http(API_URL, api_headers(), payload)
        if status != 401 or attempt:
            break
        api_token(refresh=True)  # ключ прострочився — беремо свіжий
    check_status(status)
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError("відповідь не JSON — можливо, сайт змінив API")


def load_searches():
    if not SEARCHES_FILE.exists():
        raise ConfigError(f"немає файлу {SEARCHES_FILE.name} — створіть його поруч зі скриптом")
    searches = []
    for line in SEARCHES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, url = ("", line)
        if "|" in line:
            label, url = (part.strip() for part in line.split("|", 1))
        if "ss.ge" not in url:
            continue  # рядок-заглушка або щось стороннє
        searches.append((label or "ss.ge", url))
    if not searches:
        raise ConfigError(f"у {SEARCHES_FILE.name} немає жодного посилання на ss.ge — "
                          "вставте туди адресу сторінки пошуку (інструкція всередині файлу)")
    return searches


def search_params(search_url):
    """Перетворює адресу сторінки пошуку ss.ge на параметри пошуку в API."""
    parts = urllib.parse.urlsplit(search_url.strip())
    segments = [urllib.parse.unquote(s).lower() for s in parts.path.split("/") if s]
    slugs = segments[segments.index("l") + 1:] if "l" in segments else []
    params = {}
    for key, table in (("realEstateType", REAL_ESTATE_TYPES), ("realEstateDealType", DEAL_TYPES)):
        for code, names in table.items():
            if any(name.lower() in slugs for name in names):
                params[key] = code
    for key, value in urllib.parse.parse_qsl(parts.query):
        try:
            if key in INT_LIST_PARAMS:
                params[key] = [int(v) for v in value.split(",") if v.strip()]
            elif key in INT_PARAMS:
                params[key] = int(value)
            elif key in JSON_PARAMS:
                params[key] = as_dict(json.loads(value))
            elif key == "subwayStation":
                params[key] = [v for v in value.split(",") if v]
            elif key == "searchString":
                params[key] = value
        except ValueError:
            raise ConfigError(f"у посиланні незрозуміле значення {key}={value}")
    if not params.get("cityIdList") and not params.get("municipalityId"):
        raise ConfigError(
            "у посиланні немає фільтра міста (cityIdList=...). Оберіть на сайті Батумі, застосуйте "
            "фільтри й скопіюйте адресу ще раз — у ній має бути «/l/Квартира/Аренда?cityIdList=...»")
    params["order"] = 1  # «за датою»: інакше згори стоять старі VIP-оголошення й нові тонуть
    if ONLY_OWNERS:
        params["advancedSearch"] = {**params.get("advancedSearch", {}), "individualEntityOnly": True}
    return params


def search_key(params):
    """Незмінний ключ пошуку для state.json."""
    return json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fetch_page(params, page):
    data = api_search({**params, "page": page, "pageSize": PAGE_SIZE})
    items = as_dict(data).get("realStateItemModel")
    if not isinstance(items, list):
        raise FetchError("неочікуваний формат відповіді — можливо, сайт змінив API")
    return [normalize(it) for it in items if isinstance(it, dict) and to_int(it.get("applicationId"))]


ROOMS_IN_TITLE = re.compile(r"(\d+)\s*-?\s*(?:комнат|room|ოთახ)", re.IGNORECASE)


def description_text(value):
    """Опис буває рядком (у видачі) або словником мов (на сторінці оголошення)."""
    if isinstance(value, dict):
        return value.get(LANG) or value.get("text") or value.get("allLanguageTogather") or ""
    return str(value or "")


def district_name(address):
    """«Район Старый Батуми» → «Старый Батуми». Загальне «Районы Батуми» пропускаємо."""
    name = clean(address.get("subdistrictTitle"))
    district = clean(address.get("districtTitle"))
    if not name and not district.lower().startswith("район"):
        name = district
    return re.sub(r"(?i)^район\s+", "", name)


def ss_time(value):
    """ss.ge пише 7 знаків після секунд, а Python до 3.11 розуміє лише 6."""
    return re.sub(r"(\.\d{6})\d+", r"\1", clean(value))


def whole(value):
    """21.0 → 21: ss.ge віддає цілі числа дробовими."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def ss_images(images):
    out = []
    for im in images if isinstance(images, list) else []:
        url = clean(as_dict(im).get("fileName"))
        if url.startswith("http"):  # у видачі — мініатюри «…_Thumb.jpg», повне фото — без «_Thumb»
            out.append({"large": url.replace("_Thumb.", "."), "is_main": bool(im.get("isMain"))})
    return out


def normalize(it):
    """Оголошення ss.ge → ті самі поля, що в myhome.ge. Тож повідомлення й база працюють без змін."""
    addr, price = as_dict(it.get("address")), as_dict(it.get("price"))
    rooms = ROOMS_IN_TITLE.search(clean(it.get("title")))
    return {
        "id": int(it["applicationId"]),
        "dynamic_title": clean(it.get("title")),
        "dynamic_slug": clean(it.get("detailUrl")),
        "price": {"1": {"price_total": price.get("priceGeo")}, "2": {"price_total": price.get("priceUsd")}},
        "area": whole(it.get("totalArea")),
        "room": rooms.group(1) if rooms else "",  # у видачі кімнат немає — з заголовка; картка уточнить
        "bedroom": whole(it.get("numberOfBedrooms")) or "",
        "floor": whole(it.get("floorNumber")),
        "total_floors": whole(it.get("totalAmountOfFloor")),
        "city_name": clean(addr.get("cityTitle")),
        "urban_name": district_name(addr),
        "address": " ".join(x for x in (clean(addr.get("streetTitle")), clean(addr.get("streetNumber"))) if x)
                   if clean(addr.get("streetTitle")) else "",  # сам номер будинку без вулиці нічого не каже
        "comment": description_text(it.get("description")),
        "images": ss_images(it.get("appImages")),
        "created_at": ss_time(it.get("createDate")),
        "last_updated": ss_time(it.get("orderDate")),
        "user_id": it.get("userId"),
    }


def to_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            sub = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                yield from _walk(value, sub)
            else:
                yield sub, str(key), value
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:20]):
            yield from _walk(value, f"{path}[{i}]")


KNOWN_COUNT_KEYS = ("user_statements_count", "user_statement_count", "statements_count",
                    "user_active_statements_count", "active_statements_count",
                    "userApplicationCount", "user_applications_count")
COUNT_KEY_RE = re.compile(r"(statements?|applications?|listings?|announcements?)_?count$|(^|_)ads?_?count$",
                          re.IGNORECASE)
NOT_AUTHOR_PATH_RE = re.compile(r"similar|related|recommend|view|favou?rite|photo|image|comment|like",
                                re.IGNORECASE)


def find_author_listing_count(data):
    """Шукає в даних оголошення лічильник оголошень автора на сайті.
    Повертає (число, назва поля) або (None, None)."""
    data = as_dict(data)
    for scope, prefix in ((data, ""), (as_dict(data.get("user")), "user."), (as_dict(data.get("owner")), "owner.")):
        for key in KNOWN_COUNT_KEYS:
            number = to_int(scope.get(key))
            if number is not None and 0 < number < 100000:
                return number, prefix + key
    for path, key, value in _walk(data):
        number = to_int(value)
        if (number is not None and 0 < number < 100000
                and COUNT_KEY_RE.search(key) and not NOT_AUTHOR_PATH_RE.search(path)):
            return number, path
    return None, None


def apply_author_listing_count(item, data):
    number, field = find_author_listing_count(data)
    if number is not None:
        item["_site_count"], item["_site_count_field"] = number, field


def fetch_detail(item):
    """Дані зі сторінки оголошення (applicationData)."""
    detail = as_dict(page_data(listing_url(item)).get("applicationData"))
    if not detail:
        raise FetchError("на сторінці оголошення немає даних — можливо, його вже зняли")
    return detail


def enrich_with_details(item):
    """Догружає сторінку оголошення: телефон, ім'я, кімнати, повний опис, усі фото й кількість оголошень автора."""
    apply_author_listing_count(item, item)  # раптом лічильник є вже у видачі
    try:
        detail = fetch_detail(item)
    except FetchError as e:
        log(f"  картка {item['id']}: {e}")
        return item
    if "_site_count" not in item:
        apply_author_listing_count(item, detail)
    phones = [ph for ph in detail.get("applicationPhones") or []
              if isinstance(ph, dict) and clean(ph.get("phoneNumber"))]
    phones.sort(key=lambda ph: not ph.get("isMain"))
    if phones:
        item["user_phone_number"] = clean(phones[0]["phoneNumber"])
        item["additional_phone_number"] = ", ".join(clean(ph["phoneNumber"]) for ph in phones[1:])
    for key, value in (("user_title", detail.get("contactPerson")), ("room", detail.get("rooms")),
                       ("bedroom", detail.get("bedrooms")), ("floor", detail.get("floor")),
                       ("total_floors", detail.get("floors")), ("area", detail.get("totalArea")),
                       ("user_id", detail.get("userId"))):
        if clean(whole(value)) not in ("", "0"):
            item[key] = whole(value)
    comment = description_text(detail.get("description"))
    if len(clean(comment)) > len(clean(item.get("comment"))):
        item["comment"] = comment
    images = ss_images(detail.get("appImages"))
    if len(images) > len(item.get("images") or []):
        item["images"] = images
    return item


# ─────────────────────────── аналіз оголошення ───────────────────────────

PHONE_RE = re.compile(r"(?<![\d+])(?:\+?995[\s\-.]?)?(5\d{2}(?:[\s\-.]?\d){6})(?!\d)")


def find_phone(*texts):
    for text in texts:
        match = PHONE_RE.search(str(text or ""))
        if match:
            d = re.sub(r"\D", "", match.group(1))
            return f"+995 {d[:3]} {d[3:5]} {d[5:7]} {d[7:]}"
    return ""


AGENT_WORD = re.compile(
    r"агентств\w*|агенц\w*|агент\w*|риелтор\w*|риэлтор\w*|рієлтор\w*|маклер\w*|брокер\w*|"
    r"посредни\w*|посередни\w*|комисси\w*|коміс\w*|"
    r"agenc\w*|agent\w*|realtor\w*|broker\w*|commission\w*|"
    r"სააგენტო\w*|აგენტ\w*|რიელტორ\w*|მაკლერ\w*|ბროკერ\w*|საკომისიო\w*|შუამავ\w*",
    re.IGNORECASE)
NEGATION_BEFORE = re.compile(r"\b(без|не|no|non|without)\W*$", re.IGNORECASE)
NEGATION_AFTER = re.compile(
    r"^\W*(?:\w+\W+){0,4}?(?:просьба\W+|прошу\W+|пожалуйста\W+|please\W+|გთხოვთ\W+)?"
    r"(?:не\W+(?:звон|беспок|пис|обращ|турб|дзвон|пиш)"
    r"|(?:do\W+not|don'?t|not)\W+(?:call|contact|disturb|bother|text|write)"
    r"|(?:არ|ნუ)\W|გარეშე)",
    re.IGNORECASE)


def text_signals(text):
    """(є ознаки агенції в тексті, автор пише «без посередників»)."""
    text = str(text or "")
    agent = owner = False
    for m in AGENT_WORD.finditer(text):
        before = text[max(0, m.start() - 9):m.start()]
        after = text[m.end():m.end() + 60]
        if NEGATION_BEFORE.search(before) or NEGATION_AFTER.search(after):
            owner = True   # «агентствам не звонить», «без комиссии», «no agents»...
        else:
            agent = True   # «комиссия агентства 50%», «real estate agency»...
    return agent, owner


def phone_fingerprint(digits):
    """Відбиток телефону замість самого номера.

    У state.json (він лежить у публічному репозиторії) не має бути чужих
    телефонів. Відбиток рахується з токеном бота як секретним ключем, тож
    підібрати номер перебором ззовні не вийде, а бот далі бачить, що два
    оголошення — від одного телефону.
    """
    key = (TELEGRAM_BOT_TOKEN or "ssge-watcher").encode("utf-8")
    return hmac.new(key, digits.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def author_keys(item):
    """Ключі автора: id на ss.ge та/або відбиток номера телефону."""
    keys = []
    uid = item.get("user_id") or item.get("userId") or as_dict(item.get("user")).get("id")
    if uid:
        keys.append(f"u:{uid}")
    phone = find_phone(item.get("user_phone_number"), item.get("additional_phone_number"),
                       item.get("comment"))
    if phone:
        keys.append("p:" + phone_fingerprint(re.sub(r"\D", "", phone)[-9:]))
    return keys


def author_count(state, item):
    """Скільки оголошень цього автора буде в базі разом із поточним (0 — автор невідомий)."""
    keys = author_keys(item)
    if not keys:
        return 0
    return max(state["authors"].get(k, [0])[0] for k in keys) + 1


def register_author(state, item, day):
    for k in author_keys(item):
        count = state["authors"].get(k, [0])[0]
        state["authors"][k] = [count + 1, day]


# ─────────────────────────────── повідомлення ───────────────────────────────

def fmt_num(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return clean(value)


def fmt_area(value):
    try:
        return f"{float(value):.1f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return clean(value)


def price_text(item):
    price = as_dict(item.get("price"))
    gel = as_dict(price.get("1")).get("price_total")
    usd = as_dict(price.get("2")).get("price_total")
    parts = []
    if gel:
        parts.append(f"{fmt_num(gel)} ₾")
    if usd:
        parts.append(f"${fmt_num(usd)}")
    return " · ".join(parts)


def _parse_time(raw):
    raw = clean(raw)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt.astimezone(GEORGIA_TZ) if dt.tzinfo else dt


def facts_text(item):
    """«45 м² · кімнат: 2 · спалень: 1 · поверх 5/12»."""
    facts = []
    if clean(item.get("area")):
        facts.append(f"{fmt_area(item['area'])} м²")
    if clean(item.get("room")):
        facts.append(f"кімнат: {clean(item['room'])}")
    if clean(item.get("bedroom")):
        facts.append(f"спалень: {clean(item['bedroom'])}")
    if clean(item.get("floor")):
        total = clean(item.get("total_floors"))
        facts.append(f"поверх {clean(item['floor'])}" + (f"/{total}" if total else ""))
    return " · ".join(facts)


def listing_time(item):
    """«25.09 02:01», а якщо оголошення потім піднімали — «25.09 02:01, оновлено 27.09 14:03»."""
    created = _parse_time(item.get("created_at") or item.get("create_date"))
    updated = _parse_time(item.get("last_updated"))
    if created and updated and updated.date() != created.date():
        year = "" if created.year == updated.year else ".%Y"  # на ss.ge часто піднімають торішні оголошення
        return f"{created.strftime('%d.%m' + year + ' %H:%M')}, оновлено {updated:%d.%m %H:%M}"
    moment = created or updated
    return moment.strftime("%d.%m %H:%M") if moment else ""


def listing_url(item):
    root = LISTING_ROOTS.get(LANG, LISTING_ROOTS["ru"])
    return SITE + urllib.parse.quote(f"/{LANG}/{root}/{clean(item.get('dynamic_slug')) or item['id']}")


def image_urls(item):
    """Адреси всіх фото оголошення, головне — першим."""
    images = item.get("images") if isinstance(item.get("images"), list) else []
    images = sorted(images, key=lambda im: not (isinstance(im, dict) and im.get("is_main")))
    urls = []
    for im in images:
        url = im if isinstance(im, str) else (im.get("large") or im.get("thumb")) if isinstance(im, dict) else ""
        if isinstance(url, str) and url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


def main_image(item):
    urls = image_urls(item)
    return urls[0] if urls else ""


def plural(n, one, few, many):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def build_message(item, label, count, max_len=4096):
    def esc(value):
        return html.escape(clean(value), quote=False)

    lines = [f"🏠 <b>{esc(item.get('dynamic_title')) or 'Нове оголошення'}</b>"]
    if price_text(item):
        lines.append(f"💵 {esc(price_text(item))}")

    facts = esc(facts_text(item))
    if facts:
        lines.append("📐 " + facts)

    place = ", ".join(p for p in (clean(item.get("urban_name")),
                                  item.get("_ru_address") or clean(item.get("address"))) if p)
    if place:
        lines.append(f"📍 {esc(place)}")

    who = [esc(item.get("user_title"))] if clean(item.get("user_title")) else []
    phone = find_phone(item.get("user_phone_number"), item.get("additional_phone_number"),
                       item.get("comment"))
    masked = clean(item.get("user_phone_number"))
    if phone:
        who.append(f"📞 {phone}")
    elif "*" in masked:
        who.append(f"📞 {esc(masked)} (повністю — на сайті)")
    if who:
        lines.append("👤 " + " · ".join(who))

    when = listing_time(item)
    lines.append(" · ".join(x for x in (f"🕒 {when}" if when else "", f"🔎 {esc(label)}") if x))

    agent_text, owner_text = text_signals(plain_text(item.get("comment")))
    site = item.get("_site_count")
    if isinstance(site, int):
        words = plural(site, "оголошення", "оголошення", "оголошень")
        if site >= AGENT_THRESHOLD:
            lines.append(f"⚠️ У автора {site} {words} на ss.ge — агент або інвестор")
        else:
            lines.append(f"✅ У автора {site} {words} на ss.ge")
    if count >= AGENT_THRESHOLD and (not isinstance(site, int) or count > site):
        words = plural(count, "оголошенні", "оголошеннях", "оголошеннях")
        lines.append(f"⚠️ Цей автор або телефон уже трапився в {count} {words} — агент або інвестор")
    if agent_text:
        lines.append("⚠️ В описі згадується агенція або комісія")
    elif owner_text:
        lines.append("✍️ Пише, що без посередників")

    link = f'🔗 <a href="{html.escape(listing_url(item))}">Відкрити на ss.ge</a>'
    message = "\n".join(lines)
    desc = item.get("_ru_comment") or plain_text(item.get("comment"))
    budget = min(400, max_len - visible_len(message) - visible_len(link) - 10)
    if desc and budget > 40:
        snippet = desc if len(desc) <= budget else desc[:budget - 1].rstrip() + "…"
        message += f"\n\n<i>{esc(snippet)}</i>"
    return message + "\n\n" + link


def print_listing(item, label, count):
    plain = html.unescape(re.sub(r"<[^>]+>", "", build_message(item, label, count)))
    print("-" * 60)
    print(plain.replace("Відкрити на ss.ge", listing_url(item)), flush=True)


# ─────────────────────────────── Telegram ───────────────────────────────

def chat_ids():
    return [c.strip() for c in str(TELEGRAM_CHAT_ID).split(",") if c.strip()]


def telegram_ready():
    return bool(TELEGRAM_BOT_TOKEN and chat_ids())


def _post_json(url, payload, headers=None, timeout=30):
    """POST з JSON → (код, текст відповіді). Спершу через curl_cffi, інакше — urllib."""
    if cffi_requests:
        resp = cffi_requests.post(url, json=payload, headers=headers, timeout=timeout)
        return resp.status_code, resp.text
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in range(3):
        try:
            status, body = _post_json(url, payload)
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return {"ok": False, "description": str(e)}
        try:
            result = json.loads(body)
        except ValueError:
            result = {"ok": False, "description": f"HTTP {status}"}
        wait = as_dict(result.get("parameters")).get("retry_after")
        if status == 429 and wait and attempt < 2:
            time.sleep(int(wait) + 1)
            continue
        return result
    return {"ok": False, "description": "забагато спроб"}


def send_text(text):
    for chat in chat_ids():
        result = tg_call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                         "link_preview_options": {"is_disabled": True}})
        if not result.get("ok"):
            log(f"Telegram ({chat}): {result.get('description')}")


def download_image(url):
    """Завантажує фото з сайту (для випадків, коли Telegram не може взяти його за посиланням)."""
    try:
        if cffi_requests:
            resp = cffi_requests.get(url, headers={"Referer": SITE + "/"}, impersonate="chrome", timeout=30)
            status, body = resp.status_code, resp.content
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": SITE + "/"})
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                status, body = resp.status, resp.read()
    except Exception as e:
        log(f"  фото {url}: {e}")
        return None
    if status != 200 or not body or len(body) > 10 * 1024 * 1024:
        log(f"  фото {url}: HTTP {status}, {len(body or b'')} байт")
        return None
    return body


def tg_upload(method, fields, files):
    """Запит до Telegram з файлами (multipart). files: {назва: (ім'я файлу, bytes)}."""
    boundary = f"----ssge{random.getrandbits(64):x}"
    parts = []
    for name, value in fields.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode("utf-8"))
    for name, (filename, data) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     "Content-Type: image/jpeg\r\n\r\n".encode("utf-8") + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    try:
        if cffi_requests:
            resp = cffi_requests.post(url, data=body, headers=headers, timeout=120)
            text = resp.text
        else:
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as resp:
                    text = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
        return json.loads(text)
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_photo(chat, urls, caption):
    """Фото з підписом: спершу за посиланням, інакше завантажує й шле файлом; пробує до 3 фото."""
    result = {"ok": False, "description": "немає фото"}
    for url in urls[:3]:
        result = tg_call("sendPhoto", {"chat_id": chat, "photo": url, "caption": caption,
                                       "parse_mode": "HTML"})
        if result.get("ok"):
            return result
        log(f"  sendPhoto за посиланням не вдався ({result.get('description')}) — шлю файлом")
        data = download_image(url)
        if data:
            result = tg_upload("sendPhoto", {"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                                {"photo": ("photo.jpg", data)})
            if result.get("ok"):
                return result
            log(f"  sendPhoto файлом не вдався: {result.get('description')}")
    return result


def send_listing(item, label, count):
    """Надсилає оголошення у всі чати. True, якщо дійшло хоча б в один."""
    photos = image_urls(item)
    caption = build_message(item, label, count, max_len=1024)
    full_text = build_message(item, label, count)
    if not photos:
        log(f"  оголошення {item['id']}: на сайті немає фото")
    delivered = False
    for chat in chat_ids():
        result = send_photo(chat, photos, caption) if photos else {"ok": False}
        if not result.get("ok"):  # фото не підійшло — шлемо текстом
            result = tg_call("sendMessage", {"chat_id": chat, "text": full_text,
                                             "parse_mode": "HTML"})
        if result.get("ok"):
            delivered = True
        else:
            log(f"Telegram ({chat}): {result.get('description')}")
        time.sleep(1.1)
    return delivered


# ─────────────────────────────── переклад (OpenAI) ───────────────────────────────

def openai_json(system, data):
    """Запит до OpenAI з відповіддю-JSON. Повертає dict або кидає FetchError."""
    payload = {"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
               "max_completion_tokens": 4000,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]}
    try:
        status, body = _post_json("https://api.openai.com/v1/chat/completions", payload,
                                  {"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=120)
    except Exception as e:
        raise FetchError(f"мережева помилка: {e}") from e
    try:
        data = json.loads(body)
    except ValueError:
        raise FetchError(f"HTTP {status}")
    if status != 200:
        raise FetchError(clean(as_dict(data.get("error")).get("message")) or f"HTTP {status}")
    try:
        parts = json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise FetchError("відповідь не в очікуваному форматі")
    if not isinstance(parts, dict):
        raise FetchError("відповідь не в очікуваному форматі")
    return parts


TRANSLATE_PROMPT = """Переведи на русский язык описание и адрес объявления об аренде квартиры в Батуми.
Верни JSON: {"description": "...", "address": "..."}.
Переводи точно, ничего не добавляй и не сокращай, эмодзи оставь. Названия улиц передавай
по-русски: «რუსთაველის ქ. 15» → «ул. Руставели 15», «ჭავჭავაძის ქ.» → «ул. Чавчавадзе».
Если поле пустое или уже на русском — верни его как есть."""


def needs_translation(text):
    """Грузинські літери або латиниці більше, ніж кирилиці."""
    text = str(text or "")
    if re.search(r"[\u10A0-\u10FF]", text):
        return True
    latin = len(re.findall(r"[A-Za-z]", text))
    return latin > 20 and latin > len(re.findall(r"[А-Яа-яЁёІіЇїЄє]", text))


def translate_listing(item):
    """Перекладає опис і адресу для повідомлення в чаті (item["_ru_comment"], item["_ru_address"])."""
    desc, address = plain_text(item.get("comment"))[:1500], clean(item.get("address"))
    if not (needs_translation(desc) or needs_translation(address)):
        return False
    try:
        parts = openai_json(TRANSLATE_PROMPT, {"description": desc, "address": address})
    except FetchError as e:
        log(f"  переклад {item['id']}: {e}")
        return True
    if clean(parts.get("description")):
        item["_ru_comment"] = clean(parts["description"])
    if clean(parts.get("address")):
        item["_ru_address"] = clean(parts["address"])
    return True


# ─────────────────────────────── база (state.json) ───────────────────────────────

def new_state():
    return {"version": 1, "searches": {}, "seen": {}, "authors": {}, "fail_streak": 0}


def load_state():
    if not STATE_FILE.exists():
        return new_state()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log(f"state.json пошкоджений ({e}) — починаю з нуля")
        return new_state()
    for key, default in new_state().items():
        state.setdefault(key, default)
    for key in ("tg_offset", "posted", "facts"):  # залишки кнопки «Додати в канал»
        state.pop(key, None)
    return state


def save_state(state):
    cutoff = (datetime.now(GEORGIA_TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    state["seen"] = {day: sorted(set(ids)) for day, ids in state["seen"].items() if day >= cutoff}
    state["authors"] = {k: v for k, v in state["authors"].items() if v[1] >= cutoff}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    tmp.replace(STATE_FILE)


def seen_ids(state):
    return {int(i) for ids in state["seen"].values() for i in ids}


def mark_seen(state, listing_id, day):
    state["seen"].setdefault(day, []).append(int(listing_id))


def update_fail_streak(state, any_ok):
    if any_ok:
        if state["fail_streak"] >= FAIL_ALERT_AFTER and telegram_ready():
            send_text("✅ ss.ge знову віддає дані, моніторинг працює.")
        state["fail_streak"] = 0
        return
    state["fail_streak"] += 1
    if state["fail_streak"] == FAIL_ALERT_AFTER and telegram_ready():
        send_text("⚠️ Кілька перевірок поспіль ss.ge не віддає дані. Можливо, сайт змінив "
                  "API або блокує запити — варто глянути лог запусків.")


# ─────────────────────────────── основна логіка ───────────────────────────────

def run_once(state):
    searches = load_searches()
    day = today()
    seen = seen_ids(state)
    candidates, batch, any_ok = [], set(), False

    for label, url in searches:
        try:
            params = search_params(url)
        except ConfigError as e:
            log(f"[{label}] {e}")
            continue
        key = search_key(params)
        seeding = key not in state["searches"]
        pages = SEED_PAGES if seeding else PAGES_PER_RUN

        found, ok = [], False
        for page in range(1, pages + 1):
            try:
                items = fetch_page(params, page)
            except FetchError as e:
                log(f"[{label}] сторінка {page}: {e}")
                break
            ok = True
            if not items:
                break
            found.extend(items)
            if page < pages:
                time.sleep(random.uniform(1.0, 2.0))
        if not ok:
            continue
        any_ok = True

        if seeding:  # перший запуск цього пошуку: запам'ятовуємо все, нічого не шлемо
            for item in found:
                listing_id = int(item["id"])
                if listing_id not in seen and listing_id not in batch:
                    seen.add(listing_id)
                    mark_seen(state, listing_id, day)
                    register_author(state, item, day)
            state["searches"][key] = day
            log(f"[{label}] перший запуск: запам'ятав {len(found)} оголошень, далі — лише нові")
            if telegram_ready():
                send_text(f"✅ Стежу за пошуком «{html.escape(label)}». Поточні {len(found)} "
                          "оголошень запам'ятав — надсилатиму тільки нові.")
            continue

        for item in found:
            listing_id = int(item["id"])
            if listing_id not in seen and listing_id not in batch:
                batch.add(listing_id)
                candidates.append((label, item))

    update_fail_streak(state, any_ok)
    if not candidates:
        log("нових оголошень немає" if any_ok else "не вдалося отримати дані з ss.ge")
        return

    candidates.sort(key=lambda c: int(c[1]["id"]), reverse=True)  # новіші мають більший id
    if len(candidates) > MAX_ALERTS_PER_RUN:
        log(f"нових {len(candidates)}, надсилаю {MAX_ALERTS_PER_RUN}, решту — наступного разу")
        candidates = candidates[:MAX_ALERTS_PER_RUN]
    candidates.reverse()  # у чаті йдуть від старших до новіших

    sent, translate_deadline = 0, time.time() + TRANSLATE_BUDGET_SECONDS
    for label, item in candidates:
        if FETCH_DETAILS:
            enrich_with_details(item)
            time.sleep(random.uniform(0.5, 1.2))
        if OPENAI_API_KEY and TRANSLATE_ALERTS and time.time() < translate_deadline:
            translate_listing(item)
        count = author_count(state, item)
        suspect = max(count, item.get("_site_count") or 0)
        if SKIP_SUSPECTED_AGENTS and suspect >= AGENT_THRESHOLD:
            log(f"  пропускаю {item['id']}: у автора {suspect} оголошень")
            delivered = True
        elif telegram_ready():
            delivered = send_listing(item, label, count)
        else:
            print_listing(item, label, count)
            delivered = True
        if delivered:  # не дійшло — спробуємо наступного запуску
            mark_seen(state, item["id"], day)
            register_author(state, item, day)
            sent += 1
    log(f"оброблено нових оголошень: {sent} з {len(candidates)}")


def cmd_once():
    state = load_state()
    try:
        run_once(state)
    finally:
        save_state(state)
    return 0


def cmd_loop():
    log(f"Старт. Перевірка кожні {LOOP_MINUTES} хв. Зупинити — Ctrl+C.")
    if not telegram_ready():
        log("Telegram не налаштований — оголошення друкуватимуться тут, у консолі.")
    while True:
        cmd_once()
        time.sleep(LOOP_MINUTES * 60 + random.randint(0, 45))


def cmd_test():
    label, url = load_searches()[0]
    params = search_params(url)
    log(f"[{label}] запит до ss.ge...")
    items = fetch_page(params, 1)
    cities = sorted({clean(it.get("city_name")) for it in items if clean(it.get("city_name"))})
    log(f"[{label}] на першій сторінці {len(items)} оголошень; міста: {', '.join(cities) or '—'}")
    for item in items[:3]:
        print_listing(item, label, 0)
    if not items:
        log("Порожньо. Перевірте, що за цим посиланням на сайті є оголошення.")
        return 0
    item = enrich_with_details(items[0])
    if "_site_count" in item:
        log(f"Кількість оголошень автора на сайті: {item['_site_count']} "
            f"(поле «{item['_site_count_field']}») ✅")
    else:
        log("Кількість оголошень автора в даних сайту не знайдено. Запустіть "
            "python watcher.py --dump і надішліть файли dump_list.json та dump_detail.json.")
    if telegram_ready():
        ok = send_listing(item, f"🧪 ТЕСТ · {label}", 0)
        log("Тестове повідомлення надіслано ✅" if ok else "Не вдалося надіслати в Telegram ❌")
    else:
        log("Telegram ще не налаштований: вкажіть TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID.")
    return 0


def cmd_dump():
    """Зберігає «сирі» відповіді сайту, щоб подивитися, які поля там є."""
    label, url = load_searches()[0]
    raw_list = api_search({**search_params(url), "page": 1, "pageSize": PAGE_SIZE})
    (BASE_DIR / "dump_list.json").write_text(
        json.dumps(raw_list, ensure_ascii=False, indent=2), encoding="utf-8")
    items = as_dict(raw_list).get("realStateItemModel") or []
    log(f"[{label}] збережено dump_list.json ({len(items)} оголошень)")
    if items and isinstance(items[0], dict) and to_int(items[0].get("applicationId")):
        raw_detail = fetch_detail(normalize(items[0]))
        (BASE_DIR / "dump_detail.json").write_text(
            json.dumps(raw_detail, ensure_ascii=False, indent=2), encoding="utf-8")
        log("збережено dump_detail.json (картка першого оголошення)")
    return 0


def cmd_chat_id():
    if not TELEGRAM_BOT_TOKEN:
        raise ConfigError("спершу вкажіть TELEGRAM_BOT_TOKEN")
    result = tg_call("getUpdates", {"limit": 100, "allowed_updates": [
        "message", "edited_message", "channel_post", "my_chat_member"]})
    if not result.get("ok"):
        raise ConfigError(f"Telegram відповів: {result.get('description')}")
    chats, migrated, channels = {}, set(), set()
    for update in result.get("result", []):
        for kind in ("message", "edited_message", "channel_post", "my_chat_member"):
            event = as_dict(update.get(kind))
            chat = as_dict(event.get("chat"))
            if chat.get("id"):
                name = chat.get("title") or " ".join(
                    filter(None, (chat.get("first_name"), chat.get("last_name")))) or chat.get("username")
                chats[chat["id"]] = name or ""
                if chat.get("type") == "channel":
                    channels.add(chat["id"])
            if event.get("migrate_to_chat_id"):  # група стала супергрупою й отримала новий id
                migrated.add(chat.get("id"))
                chats.setdefault(event["migrate_to_chat_id"], chats.get(chat.get("id"), ""))
    for old_id in migrated:
        chats.pop(old_id, None)
    if not chats:
        log("Порожньо. Напишіть у групі (або боту) /start і запустіть ще раз.")
        return 0
    for chat_id, name in chats.items():
        kind = "  ← канал, для оголошень не підходить" if chat_id in channels else ""
        print(f"chat_id = {chat_id}    ({name}){kind}")
    for cid in channels:
        chats.pop(cid, None)
    if not chats:
        log("Робочого чату не знайдено. Напишіть у групі (або боту) /start і запустіть ще раз.")
        return 0

    groups = [cid for cid in chats if int(cid) < 0]
    choice = groups[0] if len(groups) == 1 else (next(iter(chats)) if not groups and len(chats) == 1 else None)
    if choice is None:
        log("Знайдено кілька чатів — впишіть потрібний chat_id у telegram.json вручну.")
        return 0
    global TELEGRAM_CHAT_ID
    TELEGRAM_CHAT_ID = str(choice)
    config = {"token": TELEGRAM_BOT_TOKEN, "chat_id": TELEGRAM_CHAT_ID}
    if OPENAI_API_KEY:
        config["openai_key"] = OPENAI_API_KEY
    TELEGRAM_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    send_text("✅ Бот підключено! Сюди приходитимуть нові оголошення від власників з ss.ge.")
    log(f"chat_id {choice} ({chats[choice]}) збережено в telegram.json, у чат надіслано привітання")
    return 0


def cmd_reset():
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    log("Базу очищено. Наступний запуск тихо запам'ятає поточні оголошення.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="ss.ge -> Telegram: нові оголошення від власників")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    group.add_argument("--test", action="store_true", help="тестовий прогін")
    group.add_argument("--chat-id", action="store_true", help="показати chat_id з повідомлень боту")
    group.add_argument("--reset", action="store_true", help="забути базу побачених оголошень")
    group.add_argument("--dump", action="store_true", help="зберегти сирі відповіді сайту у файли")
    args = parser.parse_args()
    try:
        if args.chat_id:
            return cmd_chat_id()
        if args.reset:
            return cmd_reset()
        if args.test:
            return cmd_test()
        if args.dump:
            return cmd_dump()
        if args.once:
            return cmd_once()
        return cmd_loop()
    except ConfigError as e:
        log(f"Помилка налаштувань: {e}")
        return 1
    except FetchError as e:
        log(f"ss.ge: {e}")
        return 1
    except KeyboardInterrupt:
        log("Зупинено.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
