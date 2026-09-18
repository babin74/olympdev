# Olympdev

Инструментарий для олимпиадных задач: песочница, контент-адресуемое хранилище и
пайплайн проверки решений.

Пакет на ранней стадии (0.0.1). Основной интерфейс — Python API и CLI
`olympdev`.

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[notebook]"     # extra нужен только для ноутбука

olympdev new contest my-contest --problem aplusb   # рабочий скелет
olympdev run example-contest --solution sol-wa
olympdev verify example-contest          # все решения задачи, сверка с тегами
```

```
aplusb: match  (wall 16.73s)
test                 sol-ac [OK, match]  sol-wa [WA, match]  sol-tl [TL, match]
-------------------  ------------------  ------------------  ------------------
tests/sample-01.txt  OK 1.0/1.0 ms       WA 2.0/30 ms        OK 2.0/92 ms
tests/manual-01.txt  OK 1.0/1.0 ms       WA 1.0/1.0 ms       TL 4.0/2001 ms
cpu/wall             1.0/1.0 ms          2.0/30 ms           4.0/2001 ms
```

Время показывается как `<time>/<wall>` — процессорное и астрономическое.
Строка `cpu/wall` под таблицей — не сумма по тестам, а пик каждого времени
по отдельности: худший тест по CPU и худший тест по wall-time (не обязательно
один и тот же). Одного числа мало: решение, которое спит, почти не тратит CPU
(`4.0/2001 ms` выше — это именно sleep, а не тяжёлый цикл), а на загруженном
хосте астрономическое время завышено относительно реальной работы.

Из Python — то же самое:

```python
from olympdev.problem.build import build_problem_testing_input
from olympdev.problem.package import load_target
from olympdev.worker.pipeline import PipelineContext, run_pipeline
from olympdev.worker.pipelines.problem_testing import ProblemTestingPipeline

package = load_target("example-contest").problem("aplusb")
digest = build_problem_testing_input(storage, package, solution=package.solution("main"))

pipeline = ProblemTestingPipeline("aplusb")
run_pipeline(pipeline, PipelineContext(
    input_digest=digest, storage=storage, isolate_factory=isolate_factory,
))
pipeline.get_result(storage).verdict
```

Набор ролей (решение, эталон, чекер, генератор, валидатор) выводится из
`info.json`: чекер/генератор/валидатор берутся из `sources`, а эталонное решение
добавляется тогда и только тогда, когда у какого-то теста нет файла с ответом.
Команды компиляции собираются из настроек тулчейна, `testlib.h` подставляется
автоматически.

## Конфигурация

Настройки хоста читаются из TOML по первому совпадению:

1. `$OLYMPDEV_CONFIG`
2. `./olympdev.toml`
3. `~/.config/olympdev/config.toml`
4. `/etc/olympdev/config.toml`
5. встроенные значения по умолчанию

Все ключи необязательны. Справочный файл со всеми значениями —
[`olympdev.example.toml`](olympdev.example.toml); `olympdev config` печатает
действующие настройки и файл, из которого они взяты.

Разделы: `limits` (потолки лимитов), `isolate` (команда, каталог блокировок,
число box'ов, квоты), `storage` (`root`, опциональный `remote_root`),
`toolchains.cpp` / `toolchains.c` (компилятор, флаги, опциональный allowlist
флагов).

> Авторитетны настройки того хоста, который валидирует. Задача, собранная там,
> где потолки шире, будет отвергнута воркером с более узкими. Держите `limits`
> одинаковыми в пределах инсталляции — либо считайте воркер нижней границей.

## Граница доверия

| Кто | Что контролирует |
|-----|------------------|
| Участник | только байты исходника решения |
| Автор задачи | лимиты в пределах `limits`, флаги компиляции в пределах allow/denylist, argv генератора, файлы тестов внутри каталога задачи |
| Сервер | команду isolate, профили песочницы, монтирования, потолки лимитов, квоты, число box'ов |

Песочница управляется тремя фиксированными профилями (`COMPILE`, `RUN`,
`TOOL`): число процессов, окружение и монтирования заданы в них и не могут быть
расширены вызовом. Окружение хоста программе недоступно — `isolate -e` не
эмитится никогда. Разбор грамматики `--dir` намеренно приватный
(`olympdev/isolate/_dir_rules.py`).

Пути к тестам проверяются: только относительно каталога задачи, без `..`, без
symlink, с ограничением размера.

## Устройство

### Песочница

Обёртка над [isolate](https://github.com/ioi/isolate): создание box, запуск с
лимитами, разбор meta-файла. Лимиты: CPU time, wall time, extra kill, память
процесса и cgroup, размер файлов; дисковая квота box'а, если ФС её поддерживает.
Пул слотов (`IsolateFactory`) с блокировками между процессами и потоками.

Статусы isolate отображаются в вердикты: `OK`, `WA`, `TL`, `ML`, `RE`, `CE`,
`FL`, `PT`, `CF`.

### Хранилище

Контент-адресуемое хранилище по SHA-256. Записи атомарны (временный файл плюс
`os.replace`), блобы шардируются по префиксу digest'а. `CachingStorage(local,
remote)` — локальный кеш перед общим хранилищем: чего нет локально, подтягивается
из общего и остаётся рядом. Входы и выходы job/pipeline — digest'ы, а не пути на
хосте, поэтому воркеру достаточно digest'ов.

### Роли, job и pipeline

Пять ролей описаны один раз в [`olympdev/worker/roles.py`](olympdev/worker/roles.py):
типы job'ов, политики кеша, вердикт при ошибке компиляции, имя бинаря. Классы
job'ов генерируются из этой таблицы, а пайплайны ходят по ней циклом. Новая роль —
одна запись в таблице.

| Job | Назначение |
|-----|------------|
| `CompileJob` и роли | Компиляция C/C++ в sandbox (опасные флаги запрещены) |
| `SimpleRunJob` / `SolutionRunJob` / `ModelSolutionRunJob` | Запуск бинарника на одном входе |
| `ToolRunJob` / `CheckerJob` / `GeneratorJob` / `ValidatorJob` | Генератор, валидатор, чекер в стиле testlib |

Пайплайны не вызывают isolate: они только выдают готовые к запуску job'ы. Весь
прогресс лежит в таблице шагов, поэтому `save_state()` / `restore()` переносят
незавершённый прогон между процессами, не перезапуская уже сделанное.

Драйверы: `run_pipeline` (последовательный) и `run_pipeline_parallel` (пул
потоков, по box'у на job). Оба принимают `PipelineObserver` — наблюдать за
прогоном, не копируя цикл драйвера.

### Frontend

Три слоя, форматирование ровно в одном месте:

- [`frontend/view.py`](olympdev/frontend/view.py) — view-модели без HTML;
- [`frontend/text.py`](olympdev/frontend/text.py) — терминальные таблицы;
- [`frontend/html/`](olympdev/frontend/html/) — Jinja-шаблоны с общим `_base.css`.

Флаг `--html PATH` у любой команды сохраняет ту же view-модель через другой
рендерер.

### Ноутбук

```python
from olympdev.notebook import Workbench

wb = Workbench.open("example-contest", jobs=4)
p = wb.problem("aplusb")

p.run()                         # main-решение по умолчанию, PipelineRunView рисует себя сам
p.run("sol-tl", trace=True)    # решение по имени файла; карточки job'ов по мере выполнения
p.verify()                     # все решения сверяются с тегами, матрица тест × решение
```

`Workbench` владеет хранилищем, пулом песочниц, executor'ом и кешем. Пример —
[`notebooks/aplusb.ipynb`](notebooks/aplusb.ipynb).

### CLI

| Команда | Что делает |
|---|---|
| `olympdev new contest\|problem <path>` | рабочий скелет: `info.json`, gen/valid/check, сэмпл, решение |
| `olympdev schema info\|contest [--out FILE]` | JSON-схема `info.json` / `olympdev.json` |
| `olympdev run <target> [--solution NAME] [--jobs N]` | прогон задачи или контеста |
| `olympdev verify <target> [--solution NAME]` | сверка решений с тегами, матрица тест × решение (при `mode: "group-points"` — ещё и баллы по группам) |
| `olympdev statement <target> --lang ru [--contest] [--out FILE]` | условие задачи в PDF; с `--contest` — весь контест одним буклетом |
| `olympdev stress <target> --candidate NAME (--group N \| --gen-args ARGV)` | решение против эталона на растущих сидах, до первого расхождения |
| `olympdev compile-commands <target>` | compilation database для clangd/IDE |
| `olympdev show <digest>` | содержимое блоба из хранилища |
| `olympdev config` | действующие настройки и их источник |

Общие флаги: `--config PATH`, `--html PATH`, `--jobs N`, `--problem NAME`.
У `run` и `verify` есть ещё `--digests`: под таблицей печатаются digest'ы входа,
вывода и stderr каждого теста, а `olympdev show <digest>` показывает содержимое.
Так смотрят ответ на сэмпл при подготовке условия. Без флага digest печатается
только для упавших тестов — это то место, где, например, видно
`std::bad_alloc`.

`--jobs N` не ограничивается настройкой `isolate.sandbox_count`, а заменяет её:
`sandbox_count` — это лишь значение по умолчанию для `--jobs`, и песочниц
создаётся ровно столько, сколько запрошено.

**Кириллица.** Метрики шрифтов LH (T2A) TeX генерирует по требованию через
`mktextfm` и кладёт в `$TEXMFVAR`. В песочнице ни `mktextfm`, ни записи нет,
поэтому кеш монтируется из хоста только на чтение. Песочница работает не от
вашего пользователя, так что кеш должен быть ему доступен:

```bash
chmod -R a+rX "$(kpsewhich -var-value=TEXMFVAR)"
```

Монтируется именно этот каталог, права каталогов выше не проверяются — кеш
внутри `$HOME` с режимом 750 работает, сам `$HOME` открывать не нужно. Если
какой-то метрики в кеше нет, olympdev назовёт её в сообщении об ошибке;
сгенерировать её можно на хосте (`mktextfm <имя>`).

`--contest` собирает все задачи контеста в один PDF через общий шаблон
документа. Нужны две вещи: запись для этого языка в `statements` контеста и
условие на нём у **каждой** задачи — буклет не бывает наполовину собранным,
поэтому недостающий язык называется по имени задачи до запуска latex.

Метаданные буклета задаются **отдельно для каждого языка** — заголовок, место
и дата печатаются, а значит переводятся:

```json
{
    "problems": ["aplusb"],
    "statements": [
        {"lang": "en", "title": "Winter Cup", "location": "Kazan", "date": "3 March 2026"},
        {"lang": "ru", "title": "Зимний кубок", "location": "Казань", "date": "3 марта 2026"}
    ]
}
```

Форма та же, что у `statements` в `info.json`, — язык адресуется одинаково в
обоих файлах.

Схему из `olympdev schema info --out info.schema.json` можно подключить в
редакторе (в VS Code — настройка `json.schemas`); в сам `info.json` ключ
`$schema` класть нельзя — неизвестные поля отвергаются намеренно, чтобы
опечатка вроде `memory_limit_kb` не прошла молча.

## Формат контеста

Пример — [`example-contest/`](example-contest/) (задача A+B). `olympdev.json`
перечисляет задачи:

```json
{ "problems": ["aplusb"] }
```

У задачи — `info.json`: `time_limit_ms`, `memory_limit_mb`, решения, исходники
(`gen`, `check`, `valid`), сэмплы и тесты. Тест — либо файл (`input_file`), либо
генерация (`gen_cmd` + `name`). Тег `main` обязателен, если решения перечислены,
и должен быть ровно один; типы исходников не должны повторяться. Остальные
теги повторяться могут — три решения с тегом `WA` это три разные ошибки, а не
конфликт.

Решение адресуется только по имени — стему его файла (`sol-wa.cpp` →
`sol-wa`), например в `--solution` или `p.run(...)`/`p.verify(...)`. Тег —
не имя, а ожидание по вердиктам; для адресации не используется.

Тег решения задаёт, какие вердикты допустимы при верификации (`olympdev verify`
/ `p.verify()`):

- `main` / `OK` — на всех тестах только `OK`; `main` ещё и эталон, если у теста
  нет файла с ответом
- `WA`, `TL`, `ML`, `RE`, `PT` — этот вердикт обязан встретиться; `OK` тоже
  разрешён; остальные вердикты — несоответствие тегу
- `Q1+Q2+…+Qn` — токен `XX` (predictable error: `WA`/`TL`/`ML`/`RE`/`PT`)
  обязателен, `XX?` (любой двухбуквенный вердикт) необязателен. `OK` всегда
  разрешён. Пример: `TL+ML?` обязан получить `TL` и может получить `ML`
- `incor` — хотя бы один predictable error, конкретный не важен
- `ignore` — решение не запускается

Predictable error — решение успешно прогнали (скомпилировалось, не `FL`/`CF`)
и оно не `OK`.

`ML` — это срабатывание cgroup-лимита (OOM killer), то есть постепенный рост
RSS. Аллокация заведомо сверх лимита обычно даёт не `ML`, а `RE`:
`std::vector` бросает `bad_alloc`, ловить его некому, процесс делает `abort`.
Отличить одно от другого помогает stderr упавшего теста — его digest печатается
в отчёте.

В пакет входят `testlib.h` и `olymp.sty`.

## Требования

- Python ≥ 3.12
- [isolate](https://github.com/ioi/isolate) на хосте
- компилятор C/C++ (`g++` / `clang++`)

Зависимости ядра: Pydantic 2 и Jinja2 (условия и `--html` — часть ядра).
Extra `notebook`: IPython для виджетов в ноутбуке.

## Тесты

```bash
pip install pytest
pytest tests -m "not slow"   # без isolate
pytest tests                 # включая sandbox и example-contest/aplusb
pytest tests --run-attacks   # ещё и tests/attacks (см. ниже)
```

Маркер `slow` — тесты, которые реально компилируют и гоняют программы в isolate.

В `tests/attacks/` — прогоны вредоносных payload (macro bomb, include хоста,
TL/ML) через тот же пайплайн, что и обычное решение. **По умолчанию они не
запускаются**, даже при полном `pytest tests`: это настоящие fork-бомбы и
пожиратели памяти, и на машине с неверно настроенным isolate их запуск сам по
себе является атакой. Включаются только явным `--run-attacks`.

## Пока нет

- Постоянный кеш job'ов на диске
- Удалённый бэкенд хранилища (интерфейс готов, реализации HTTP/S3 нет)
- Веб-сервер и UI
