# benchmark_vpn_clob.sh — замер задержки до Polymarket CLOB по VPN-профилям

Рядом лежит скрипт `benchmark_vpn_clob.sh`: он перебирает **профили NetworkManager** (типы `vpn`, `wireguard`, `openvpn`, `pptp`, `l2tp`, `openconnect`), по очереди поднимает каждый, делает несколько HTTPS-запросов к CLOB и в конце выводит **сводную таблицу** (среднее / мин / макс время ответа в секундах).

## Что нужно

- Ubuntu (или другой дистрибутив) с **NetworkManager** и `nmcli`.
- Установленные **curl**.
- Пароли/ключи VPN **сохранены в профиле NM**, чтобы `nmcli connection up` **не спрашивал** пароль в интерактиве. Иначе профиль помечается как ошибка `connection up FAILED`.

## Запуск

Из корня репозитория:

```bash
chmod +x hft_bot/scripts/benchmark_vpn_clob.sh
./hft_bot/scripts/benchmark_vpn_clob.sh
```

```bash
chmod +x scripts/benchmark_vpn_clob.sh
./scripts/benchmark_vpn_clob.sh
```

Или с каталога `hft_bot/scripts`:

```bash
chmod +x benchmark_vpn_clob.sh
./benchmark_vpn_clob.sh
```

```bash
BENCHMARK_RUNS=10 VPN_SETTLE_SEC=5 \
  CLOB_URL='https://clob.polymarket.com/' \
  ./hft_bot/scripts/benchmark_vpn_clob.sh
```

## Переменные окружения

| Переменная        | По умолчанию | Смысл |
|-------------------|--------------|--------|
| `CLOB_URL`        | `https://clob.polymarket.com/` | URL для `curl` (полный HTTPS-запрос) |
| `BENCHMARK_RUNS` | `7`         | Сколько замеров подряд на один VPN |
| `VPN_SETTLE_SEC`  | `4`          | Пауза после `connection up` до первого замера (сек) |

Пример:

```bash
BENCHMARK_RUNS=10 VPN_SETTLE_SEC=5 ./hft_bot/scripts/benchmark_vpn_clob.sh
```

## Как читать таблицу

- **avg_s** — среднее время **полного** запроса `curl` (DNS + TCP + TLS + ответ), в секундах; **меньше — лучше**.
- **min_s / max_s** — разброс по прогонам.
- **ok** — `1` если профиль поднялся, `0` если нет.
- **ok_runs** — сколько успешных числовых замеров (не `nan`).

Это **не** то же самое, что чистый «пинг»: ICMP к CDN часто бесполезен; здесь замер ближе к реальному доступу к API по HTTPS.

## Как не попасть в ловушки

- Скрипт **переключает VPN**: перед каждым профилем снимает остальные VPN из списка. В конце напоминает, если до запуска был активен другой VPN — **подключите нужный вручную**.
- Если VPN **не** через NetworkManager (например чистый `wg-quick` или сторонний клиент), скрипт их **не видит** — нужен импорт в NM или отдельный сценарий.
- Запуск с **sudo** обычно не нужен; если профили system-wide и NM не даёт поднять пользователю — смотрите права Polkit / `nmcli` от вашего пользователя.

## Ограничение ответственности

Скрипт только поднимает профили и меряет задержку. Соблюдайте правила сервисов и VPN-провайдера.
