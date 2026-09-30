# ssge-watcher

Бот стежить за пошуком на ss.ge (оренда квартир у Батумі, лише власники) і шле **нові**
оголошення в робочу Telegram-групу «Скрапер Myhome.Ge (власники)» — ту саму, що й бот
myhome, але **окремим ботом** з власним токеном. Уся логіка — в одному файлі `watcher.py`.
Мова коду, коментарів і повідомлень — українська.

## Близнюк: ~/Desktop/myhome-watcher

Цей проєкт — копія `myhome-watcher` (github.com/romario00724-glitch/myhome-watcher), у якій
замінено лише шар роботи з сайтом (розділ `# ── ss.ge ──` у `watcher.py`). `normalize()`
перекладає оголошення ss.ge у ті самі поля, що в myhome (`dynamic_title`, `urban_name`,
`price["1"]["price_total"]`, `user_phone_number`…), тому повідомлення, позначки, база,
переклад і команди — той самий код. Користувач хоче, щоб боти працювали **один в один**:
змінюєш спільну частину тут (Telegram, вигляд повідомлення, позначки, `state.json`,
workflow) — спитай, чи повторити в myhome-watcher, і навпаки.

Свідомі відмінності від myhome: рік у даті, якщо оголошення подане не цього року
(`listing_time` — на ss.ge часто «піднімають» торішні); `whole()` для чисел, які ss.ge дає
дробовими; адреса без назви вулиці не показується. Кнопки «Додати в канал» немає й не
повертати — у myhome її прибрали 30.09.2026 (див. CLAUDE.md там).

## Як запускається

```
cron-job.org ──► workflow_dispatch ──► GitHub Actions: python watcher.py --once
 :05 :15 :25…                           один прохід по ss.ge (нові оголошення → група), ~30 с
```

- **Будильник — cron-job.org**, завдання `ssge-watcher` (клон `myhome-watcher`), хвилини
  5,15,…,55 — зсув на 5 хв від бота myhome. `cron:` у `watch.yml` (о :08, :18…) — запасний.
- Токен будильника — той самий fine-grained `cron-myhome` з доступом до обох репозиторіїв
  (`Actions: Read and write`), **спливає 28.09.2027** — тоді замовкнуть обидва боти.
- Запуски по одному (`concurrency: ssge-watcher`), checkout з `ref: ${{ github.ref_name }}`.
- Після запуску workflow комітить `state.json` у `main` — локальний `main` відстає: `git pull`.

## Як бот читає ss.ge

- **Ключ до API** — гостьовий JWT (`client_id: ssweb`, діє 1 год), який сайт кладе в кожну
  сторінку: `__NEXT_DATA__` → `props.pageProps.credentialsToken`. Бот бере його з головної
  (`/ru/`, ~130 КБ стисненого) раз на запуск; на `401` бере свіжий. Легшої сторінки з ключем немає.
- **Пошук** — `POST https://api-gateway.ss.ge/v1/RealEstate/LegendSearch` з `Authorization:
  Bearer …`. API не за Cloudflare (сервер Kestrel); за Cloudflare лише сторінки `home.ss.ge`.
  **Обов'язково `Accept-Language: ru`**, інакше назви й описи приходять англійською.
- Тіло запиту — параметри з адреси сторінки пошуку під тими самими назвами (`search_params`):
  тип і угода з шляху (`/l/Квартира/Аренда` = `realEstateType: 5`, `realEstateDealType: 1`;
  таблиці для ru/en/ka узяті з JS сайту), `cityIdList` (Батумі = 96) тощо.
- Бот сам додає `advancedSearch: {"individualEntityOnly": true}` («От собственника» — працює
  **лише всередині advancedSearch**, на верхньому рівні сервер його мовчки ігнорує) і
  `order: 1` («за датою», за `orderDate`; без нього згори старі VIP). `offerType` — це тип
  платного просування, а не власник.
- **Картка** — HTML-сторінка оголошення (`/ru/недвижимость/{detailUrl}` або просто `/{id}`),
  `pageProps.applicationData`: `userApplicationCount` (оголошень у автора — підхоплює
  `KNOWN_COUNT_KEYS`), `userEntityType` (`Individual` / `Broker`…), `applicationPhones`
  (повні номери), `contactPerson`, `rooms`, `description` — словник `ka`/`en`/`ru`.
  Окремого легкого API для картки немає (`/v1/RealEstate/details` — лише PUT для власника).
- Фото: у видачі мініатюри `…_Thumb.jpg`, повне — те саме без `_Thumb`.
- Дати з 7 знаками після секунд — `ss_time()` обрізає до 6.

## Секрети й налаштування

GitHub → Settings → Secrets and variables → Actions:

| Назва | Що це |
|---|---|
| `TELEGRAM_BOT_TOKEN` (secret) | токен **цього** бота (не myhome). Він же ключ HMAC для відбитків телефонів у `state.json` (`phone_fingerprint`): заміна токена = лічильник «телефон уже траплявся» почне рахувати заново |
| `TELEGRAM_CHAT_ID` (secret) | та сама робоча група, що в myhome |
| `OPENAI_API_KEY` (secret) | **не потрібен**: ss.ge сам дає опис російською. Лише запасний перекладач |

Локально — `telegram.json` у корені (`token`, `chat_id`, `openai_key`), у `.gitignore`.
Репозиторій **публічний**: жодних ключів у коді й комітах.

## Правила роботи з кодом

- **Не запускати `python watcher.py` на комп'ютері паралельно з GitHub** — у двох копій різні
  `state.json`, і ті самі оголошення прийдуть у групу двічі.
- **`import watcher` + наявний `telegram.json` = реальні повідомлення** в робочу групу. У тестах
  глушити `tg_call`, `tg_upload`, `download_image`, `_post_json` (OpenAI) і `save_state` (або
  підмінити `STATE_FILE`). Без `telegram.json` і змінних `TELEGRAM_*` бот лише друкує в консоль —
  так безпечно ганяти `--test` і `--once` проти живого ss.ge (підмінивши `STATE_FILE`).
- Користувач не програміст: пояснювати простими словами, українською.
