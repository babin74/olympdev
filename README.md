# Olympdev

Инструментарий для олимпиадных задач: песочница, контент-адресуемое хранилище и пайплайн проверки решений.

Пакет на ранней стадии (0.0.1). Основной интерфейс — Python API; CLI пока умеет только генерировать compilation database для IDE.

## Возможности

### Песочница (isolate)

Обёртка над [isolate](https://github.com/ioi/isolate): создание box, запуск с лимитами, разбор meta-файла.

- Лимиты: CPU time, wall time, extra kill, память процесса и cgroup, размер файлов.
- Дисковая квота box (blocks/inodes), если ФС её поддерживает.
- Правила монтирования каталогов (`--dir`), cgroup-режим.
- Пул слотов (`IsolateFactory`, 1–999), блокировки между процессами и потоками.
- Команда isolate задаётся переменной `OLYMPDEV_ISOLATE_CMD` (по умолчанию `isolate`).

Статусы isolate отображаются в вердикты: `OK`, `WA`, `TL`, `ML`, `RE`, `CE`, `FL`, `PT`, `CF`.

### Хранилище

Контент-адресуемое хранилище по SHA-256: файлы, байты и JSON-словари. Реализация на диске — `FileStorage`. Входы и выходы job/pipeline — digest'ы, а не пути на хосте.

### Job и pipeline

Проверка собирается из атомарных job. Каждый job знает свой `job_type`, кладёт вход в storage и возвращает digest результата.

| Job | Назначение |
|-----|------------|
| `CompileJob` и роли (`Solution`, `Model`, `Checker`, `Generator`, `Validator`) | Компиляция C/C++ в sandbox (g++/gcc/clang; опасные флаги вроде `-fplugin` запрещены) |
| `SimpleRunJob` / `SolutionRunJob` / `ModelSolutionRunJob` | Запуск бинарника на одном входе с лимитами задачи |
| `ToolRunJob` / `CheckerJob` / `GeneratorJob` / `ValidatorJob` | Генератор, валидатор, чекер в стиле testlib |

`JobExecutor` умеет кешировать исходы по `(input_digest, policy)`. Сейчас есть `NullCacheStorage` и `MemoryCacheStorage`.

Пайплайны не вызывают isolate сами: они только выдают готовые к запуску job. Последовательный драйвер — `run_pipeline`; статус `WAITING` рассчитан на будущий параллельный раннер.

### Пайплайн проверки задачи

`ProblemTestingPipeline` прогоняет решение по всем тестам задачи:

1. Компиляция решения, чекера и при необходимости эталона, генератора, валидатора.
2. Для каждого теста (`ProblemTestPipeline`):
   - взять готовый вход **или** сгенерировать его (`gen_cmd`);
   - прогнать валидатор, если он задан;
   - запустить решение;
   - взять эталонный ответ из файла **или** с эталонного решения;
   - сравнить чекером (коды выхода testlib: 0 → OK, 1/2 → WA, 7 → PT, иначе CF).
3. Свести вердикты тестов в общий результат.

Ошибки компиляции решения дают `CE`, сбой чекера/генератора/валидатора — `CF`, сбой эталона — `FL`.

### Формат контеста

Пример лежит в `example-contest/` (задача A+B).

`olympdev.json` перечисляет задачи:

```json
{ "problems": ["aplusb"] }
```

У задачи — `info.json`: лимиты, решения (`main` / `wa` / `tl` / …), исходники (`gen`, `check`, `valid`), сэмплы и тесты. Тест — либо файл (`input_file`), либо генерация (`gen_cmd` + `name`). Пути к тестам проверяются: только относительно каталога задачи, без `..` и symlink, с ограничением размера.

В пакет входят `testlib.h` и `olymp.sty`.

### CLI

Генерация `compile_commands.json` для clangd/IDE (флаги `-O2`, include на `testlib.h` и `source/`):

```bash
python -m olympdev.frontend.cli example-contest/olympdev.json
# или для одной задачи:
python -m olympdev.frontend.cli example-contest/aplusb/info.json
```

Путь к базе компиляции задаётся в `info.json` как `dev.compile_options_path`.

### HTML

Jinja2-карточка job (статус, описание, метки, digest) — для отладки и будущих отчётов, не полноценный веб-интерфейс.

## Требования

- Python ≥ 3.12
- [isolate](https://github.com/ioi/isolate) на хосте
- компилятор C/C++ (`g++` / `clang++`) для проверки и тестов

Зависимости пакета: Pydantic 2 и Jinja2.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Тесты

```bash
pip install pytest
pytest tests -m "not slow"   # без isolate
pytest tests                 # включая sandbox и example-contest/aplusb
```

Маркер `slow` — тесты, которые реально компилируют и гоняют программы в isolate. В `tests/attacks/` — прогоны вредоносных payload (macro bomb, include хоста, TL/ML) через тот же пайплайн, что и обычное решение.

## Лимиты по умолчанию

| Параметр | Максимум |
|----------|----------|
| CPU time | 300 с |
| Wall time | 600 с |
| Память | 4096 МиБ |
| Размер файла | 256 МиБ |
| Тестов на задачу | 1024 |
| Размер файла теста | 32 МиБ |

## Пока нет

- CLI «прогнать контест / задачу»
- Генерация условий (в пакете есть `olymp.sty`, пайплайна нет)
- Постоянный кеш на диске и параллельный раннер
- Веб-сервер и UI
