# Embetty Noctalia

**Embetty Noctalia** — встроенный терминал для **Noctalia Shell 5.3+**
(`plugin_api = 33`): панель внутри оболочки, именованные PTY-сессии и защита
от опасных команд.

Это не эмулятор терминала вроде kitty. Панель показывает очищенный текстовый
вывод и отправляет введённую строку в настоящий PTY. Обычные команды, `cd`,
pipe, перенаправления и построчные интерактивные приглашения работают;
полноэкранные TUI-программы — нет (подробности ниже).

## Возможности

- Панель внутри Noctalia, без отдельного окна терминала.
- Именованные сессии: создать, переключить, переименовать, удалить.
- Каждая сессия — отдельный интерактивный shell в PTY.
- Сессии живут в фоновом демоне: закрытие панели, перезапуск плагина и
  перезапуск Noctalia не убивают запущенные процессы и не стирают историю.
- История вывода и текущий каталог сохраняются на диск. После перезапуска
  демона сессия «спит» и просыпается при следующей команде.
- Настройки: оболочка, стартовая команда, размер истории, лимит сессий,
  шрифт, защита, иконка виджета.
- Быстрое обновление вывода: демон публикует готовый снимок экрана, а
  маленький helper через `noctalia.runStream` сообщает панели об изменениях.
  Панель рисует последние до 4000 символов, полная история остаётся для
  копирования. Резервный путь — тик 1 Гц и покадровый опрос, если helper
  недоступен.
- Русский и английский интерфейс (зависит от языка Noctalia).
- Виджет для бара и плитка Control Center.
- IPC для открытия панели и выполнения команд.

## Ограничения

- Нет эмуляции экрана: ANSI-последовательности вырезаются, цвета и курсор не
  поддерживаются. Вывод — обычный текст.
- Полноэкранные TUI-программы (`vim`, `htop`, `mc`, `less` в alternate screen)
  не отрисовываются. Для них используйте обычный терминал.
- Ввод однострочный. Кнопка `Tab` отправляет TAB в PTY, но не дополняет текст,
  который ещё находится в поле ввода панели.
- Визуальное окно панели — последние до 4000 символов. Более старый вывод
  (до `scrollback_chars`) остаётся в снимке и копируется кнопкой целиком, но
  не прокручивается в панели.
- Пароли и другие построчные приглашения (`sudo`, `ssh`, `passwd`, `git`) можно
  вводить в то же поле команды. Панель распознаёт приглашение, включает скрытый
  ввод с точками, показывает замок и кнопку «показать/скрыть». Пароль не
  попадает в историю и не восстанавливается при ошибке. В самом PTY он, как и в
  обычном терминале, не эхоируется.
- Мышь, escape-последовательности и управление курсором в программах не
  поддерживаются.
- Защита от опасных команд — это **эвристический guardrail, а не песочница**.
  См. раздел «Безопасность».

## Требования

- Noctalia Shell 5.3 или новее (плагин использует API 33).
- Linux, Python 3.10+ (только стандартная библиотека, без pip-пакетов).
- `bash` (по умолчанию), `zsh`, `fish` или другая интерактивная оболочка.
- Для сборки из исходников: `noctalia` CLI. Luau-анализатор не обязателен.

## Установка

### Локальная разработка (симлинк)

```bash
mkdir -p ~/.local/share/noctalia/plugins
ln -sfn "$PWD" ~/.local/share/noctalia/plugins/embetty-noctalia

noctalia plugins lint "$PWD"
noctalia msg plugins list
noctalia msg plugins enable ayone/embetty-noctalia
```

Локальная копия в `~/.local/share/noctalia/plugins/` имеет приоритет над
источниками плагинов. Noctalia перезагружает `.luau`-файлы автоматически;
изменения `plugin.toml` подхватываются при перезагрузке конфигурации.

### Обычная установка

Скопируйте каталог плагина (без симлинка), чтобы он не зависел от рабочей
директории:

```bash
mkdir -p ~/.local/share/noctalia/plugins
cp -a /path/to/embetty-noctalia ~/.local/share/noctalia/plugins/embetty-noctalia
noctalia msg plugins enable ayone/embetty-noctalia
```

### Открыть панель

```bash
noctalia msg panel-open ayone/embetty-noctalia:terminal
noctalia msg panel-toggle ayone/embetty-noctalia:terminal
```

Горячая клавиша в Niri:

```kdl
binds {
    Mod+Return { spawn "noctalia" "msg" "panel-toggle" "ayone/embetty-noctalia:terminal"; }
}
```

Виджет в баре: добавьте `ayone/embetty-noctalia:widget` в список виджетов бара
(например, в `[bar.default] end = [...]`). Плитка Control Center:
`ayone/embetty-noctalia:shortcut`.

## Использование

- **Новая сессия** — кнопка `+`, имя 1–40 символов, без `/` и `\`.
- **Переключение** — выпадающий список сверху.
- **Переименование/удаление** — кнопки с карандашом и корзиной.
- **Закрытие панели** (крестик или `Esc`) не завершает сессию. Откройте панель
  снова — shell, cwd и история на месте.
- **Enter** — выполнить строку. **Кнопка истории** подставляет предыдущую
  команду. **Ctrl+C**, **Tab**, **Esc** отправляются в PTY.
- **Очистить** стирает историю вывода текущей сессии, **перезапустить**
  запускает новый shell в той же сессии (история сохраняется), **копировать**
  кладёт весь вывод в буфер обмена.
- Статус показывает `готова`, `выполняется: <команда>`, `спит` или `завершена`.
  Справа отображается текущий каталог.

Сессии хранятся в демоне. Демон запускается по требованию и переживает
закрытие панели и перезапуск Noctalia. При выходе из системы/перезагрузке
процессы завершаются, но метаданные и история остаются: после следующего
запуска сессия восстановится и продолжит работу при вводе команды.

## Настройки

**Settings → Plugins → Embetty Noctalia** (шестерёнка на строке плагина).

| Ключ | По умолчанию | Описание |
|---|---|---|
| `shell` | `bash` | Оболочка: имя в `PATH` или абсолютный путь. |
| `startup_command` | пусто | Команда после создания новой сессии. |
| `scrollback_chars` | `16000` | Сколько последних символов вывода хранить и отдавать панели. На экран выводятся последние до 4000 символов, но копирование берёт весь снимок. |
| `max_sessions` | `12` | Максимум именованных сессий (1–32). |
| `block_destructive` | `true` | Блокировать опасные команды. |
| `font_family` | `DejaVu Sans Mono` | Моноширинный шрифт вывода. |
| `font_size` | `12` | Размер текста, логические px. |
| `glyph` | `terminal-2` | Иконка виджета (Tabler/Nerd Font). |
| `show_label` | `false` | Показывать название рядом с иконкой. |

Стартовая команда выполняется только при создании сессии; изменение настройки
не перезапускает существующие сессии.

## Безопасность

По умолчанию плагин блокирует команды, которые с высокой вероятностью уничтожат
систему или домашний каталог. Проверка выполняется в Python до отправки строки
в PTY. Примеры блокируемых команд:

```bash
sudo rm -rf /
rm -rf ~
rm -rf /usr
rm -rf /{bin,etc}
rm --no-preserve-root -rf /
mv /etc /tmp
chmod -R 000 /
dd if=/dev/zero of=/dev/sda
mkfs.ext4 /dev/sda
wipefs -a /dev/sda
shred /dev/sda
find / -delete
echo x > /dev/sda
echo x > /etc/passwd
bash -c 'rm -rf /'
busybox rm -rf /
:(){ :|:& };:
```

Проверяются сегменты shell-команд, обёртки (`sudo`, `env`, `command`, `xargs`
и др.), подстановки `$(...)` и обратные кавычки, перенаправления, а также
защищённые системные каталоги и корень домашнего каталога. Пути разрешаются
относительно cwd сессии.

Ввод пароля не разбирается как shell-команда: пароль может содержать кавычки и
другие спецсимволы. Демон принимает такой ввод только когда на переднем плане
находится не оболочка, а ждущий процесс (`sudo`, `ssh`, `passwd`, `git` и т.п.).
Попытка отправить сырой ввод в shell отклоняется.

### Чего защита не делает

Это не sandbox и не замена правам ОС. Защиту можно обойти, например:

- переменными, алиасами, функциями и сгенерированными скриптами;
- интерпретаторами: `python3 -c '...'`, `perl`, `ruby`, `node`;
- произвольными бинарниками и скриптами, которые сами удаляют файлы;
- `cp`, `tee`, `install` и другими инструментами записи, которые не
  анализируются;
- сложными конструкциями shell, которые эвристика не разобрала.

Защищаются только системные каталоги и **весь** домашний каталог целиком;
отдельные пользовательские файлы и подкаталоги не защищены. Для недоверенных
команд используйте отдельного пользователя, контейнер или виртуальную машину и
держите резервные копии. Отключить защиту можно настройкой
`block_destructive`, но делать это не рекомендуется.

## Данные, демон и IPC

Каталог данных плагина:

```text
~/.local/state/noctalia/plugins/data/ayone/embetty-noctalia/
├── termd.sock       # Unix-сокет демона (0600)
├── termd.pid
├── termd.log        # лог демона
├── sessions.json    # имена, cwd, shell, даты создания
├── logs/<id>.log    # сохранённая история каждой сессии
├── ui/<id>.json       # быстрый снимок экрана для открытой панели
├── ui/follow-current  # id сессии, за которой следит helper панели
├── requests/          # временные JSON-запросы от Luau
├── bashrc           # сгенерированный rc для bash
└── last_session     # последняя открытая сессия
```

Демон владеет одной интерактивной оболочкой на сессию и слушает Unix-сокет.
Luau-часть запускает `backend/termctl.py` через `noctalia.runAsync` в
argv-форме; клиент при необходимости поднимает демон. Данные не пишутся в
каталог плагина, поэтому обновления плагина их не стирают.

Для отрисовки панель не запускает Python на каждый кадр. Демон сам раз в
~50 мс атомарно обновляет `ui/<id>.json` для изменившихся сессий. При первом
открытии панели Luau запускает маленький `backend/follow.py` через
`noctalia.runStream`; helper следит за `ui/follow-current` и mtime выбранного
снимка и присылает панели только id сессии (или heartbeat). Панель сама
читает JSON и перерисовывает только при изменении, не чаще 10 раз в секунду.
При закрытии панели helper переводится в паузу (пустой `follow-current`), а
при перезагрузке или отключении плагина завершается. Пока runtime плагина
загружен, helper может оставаться в памяти после закрытия панели, но в паузе
почти не потребляет CPU.

Если helper не запустился или пропал, панель переходит на резервный режим:
`update()` раз в секунду и покадровый опрос с ограничением 20 Гц. Это
медленнее push-режима, но терминал продолжает работать. Полный снимок
хранится в `output` для копирования; на экран выводится последний
`UI_VISIBLE_CHARS` (до 4000, но не больше `scrollback_chars`) символов,
чтобы не перегружать программный рендер.
Снимки — это только кэш интерфейса: их можно удалить при остановленном
демоне, они создадутся снова. Обычные команды, пароли и защита по-прежнему
проходят через один запрос к демону на действие, а не на кадр.

IPC (панель должна быть открыта для `run`/`clear`/`restart`/`new`/`session`):

```bash
noctalia msg plugin ayone/embetty-noctalia:terminal all run "echo привет"
noctalia msg plugin ayone/embetty-noctalia:terminal all clear
noctalia msg plugin ayone/embetty-noctalia:terminal all restart
noctalia msg plugin ayone/embetty-noctalia:terminal all new "work"
noctalia msg plugin ayone/embetty-noctalia:terminal all session "work"
noctalia msg plugin ayone/embetty-noctalia:service all ping
noctalia msg plugin ayone/embetty-noctalia:service all shutdown
```

`shutdown` останавливает демон и все оболочки; история и метаданные остаются.
При следующем запросе демон запустится снова, сессии будут доступны.

## Удаление

```bash
noctalia msg plugin ayone/embetty-noctalia:service all shutdown
noctalia msg plugins disable ayone/embetty-noctalia
rm -rf ~/.local/share/noctalia/plugins/embetty-noctalia
rm -rf ~/.local/state/noctalia/plugins/data/ayone/embetty-noctalia
```

Отключение плагина **не** останавливает демон — это сделано специально, чтобы
сессии переживали выключение и повторное включение. Демон останавливается при
удалении плагина (если entry был загружен) или вручную командой выше.

## Диагностика

- `python3 is required` — установите Python 3 и проверьте `PATH`.
- `configured shell was not found` — проверьте имя/путь оболочки в настройках.
- `Backend error` — смотрите `termd.log` в каталоге данных.
- Панель открывается, но пусто — проверьте, что демон жив:
  `noctalia msg plugin ayone/embetty-noctalia:service all ping`.
- `socket path is too long` — путь к данным слишком длинный для Unix-сокета
  (лимит около 100 байт). Задайте более короткий `XDG_STATE_HOME`.
- После обновления плагина старый демон продолжает работать со старым кодом.
  Перезапустите его: `noctalia msg plugin ayone/embetty-noctalia:service all shutdown`,
  затем откройте панель. Это особенно важно после обновления с версии 1.0.0:
  старый демон не умеет публиковать быстрые снимки, и панель будет обновляться
  только запасным медленным путём.
- Виджет не виден — добавьте `ayone/embetty-noctalia:widget` в конфигурацию бара.
- Нет цветов/TUI — это ограничение архитектуры, а не ошибка.

## Разработка

```bash
noctalia plugins lint .
python3 -m unittest discover -s tests -v
python3 -m py_compile backend/*.py
luau-compile --only-parse panel.luau widget.luau shortcut.luau service.luau lib/backend.luau
```

Быстрая проверка backend без Noctalia:

```bash
python3 backend/termctl.py --data /tmp/termtest --request <(printf '%s' '{"cmd":"ping"}')
```

## English summary

An embedded Noctalia Shell panel backed by a Python PTY daemon. Sessions are
named, persistent, and survive closing the panel or restarting Noctalia.
Ordinary line-oriented commands work; full-screen TUI apps are not rendered.
The daemon publishes session snapshots; a small `backend/follow.py` helper
started through `noctalia.runStream` notifies the panel when the selected
snapshot changes, so display updates do not spawn a Python process per frame.
The panel renders up to the last 4000 characters while keeping the full snapshot
for copying, with a 1 Hz/frame-tick fallback if the helper is unavailable.
A heuristic safety guard blocks classic destructive commands such as
`sudo rm -rf /`, but it is not a sandbox. See the Russian sections above for
installation, settings, data paths, IPC, and limitations.

## Лицензия

MIT.
