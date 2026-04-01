# prjBH_hft_bot

Polymarket HFT bot — отдельный репозиторий, история извлечена из `prjBJ_arb_polymarket` только по пути `hft_bot/` (`git filter-repo --subdirectory-filter hft_bot`).

## Запуск

```bash
uv sync --all-groups
uv run python bot.py
# или
uv run hft-bot
```

Тесты:

```bash
uv run pytest tests/
```

Опционально скрипты с графиками: `uv sync --extra scripts`.

## История и соответствие монорепозиторию

Коммит монорепозитория `bd7ac4f8ee6005bf0d7f54392958cbd5020c5565` (первое появление `hft_bot/`) соответствует дереву в этом репозитории в коммите **`3638c01089b6ecdca2377c8647e6079c40e75e4f`**.

В монорепозитории ветка **`main_short`** указывает на корневой коммит до выноса бота (`6c32aad…`), для справки по «чистому» дереву арбитражного проекта без последующей истории.

Подробности: [docs/GIT_HISTORY.md](docs/GIT_HISTORY.md).
