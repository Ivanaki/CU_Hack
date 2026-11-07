# Где живёт продукт и как им управлять

Боевой стенд поднят и работает сам. Эта страница — для тех, кому нужно
посмотреть логи, перезапустить сервис или выкатить правку, не дёргая никого.

## Адреса

| | |
|---|---|
| Приложение и API | `https://grosh.cu3rd.ru` |
| Бот | `@ICEQ_cu_hackBot` |
| Хост | `YOUR_SERVER_IP`, пользователь `deploy`, Франкфурт |
| Порт на хосте | `8091` — домен проксируется на него, сертификат уже настроен |
| Каталог | `~/honest-month` |

Почему не на домашнем сервере команды: он физически не достаёт до
`api.telegram.org`, блокирует провайдер. Проверено и из контейнера, и с хоста.
Бот там не работал бы ни при каких настройках.

## Доступ

Заходить по ключу. Добавьте себе в `~/.ssh/config`:

```
Host grosh
    HostName YOUR_SERVER_IP
    User deploy
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
```

Ключ на сервер кладёт любой, у кого уже есть доступ:

```bash
ssh grosh 'printf "%s\n" "<ваш ~/.ssh/id_ed25519.pub>" >> ~/.ssh/authorized_keys'
```

Пароль от учётной записи есть у команды, но раздавать его не стоит: он один
на всех и отозвать его у одного человека нельзя, только сменить всем сразу.

**Важно.** Пользователь состоит в группе `docker`, а это фактически права root
на всей машине. Машина общая: на ней работают чужие сервисы, около десятка
контейнеров. Не трогайте ничего, кроме `honest-month`.

## Повседневное

```bash
ssh grosh 'cd honest-month && docker compose ps'            # жив ли
ssh grosh 'cd honest-month && docker compose logs -f'       # логи живьём
ssh grosh 'cd honest-month && docker compose logs --tail 50'
ssh grosh 'cd honest-month && docker compose restart'       # перезапустить
curl -s https://grosh.cu3rd.ru/healthz                      # проверка снаружи
```

Сервис поднимается сам: у контейнера `restart: unless-stopped`, docker включён
в автозагрузку. Перезагрузка сервера переживается без участия человека.

## Выкатить правку

С машины, где лежит код:

```bash
tar czf - -C . --warning=no-file-changed \
  --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
  --exclude=.pytest_cache --exclude=.ruff_cache \
  --exclude=data/runtime --exclude=data/uploads \
  --exclude='*.db' --exclude=.env --exclude=pitch --exclude=pitch2 . \
  | ssh grosh 'tar xzf - -C honest-month'

ssh grosh 'cd honest-month && docker compose up -d --build'
```

`.env` намеренно не заливается: на сервере он свой, с боевыми адресами.
Перед выкаткой прогоните `python -m pytest -q` — набор должен быть зелёным.

Проверить после выкатки:

```bash
python -m scripts.smoke --base https://grosh.cu3rd.ru --tg-id 1
```

Ожидаемо «34 из 34, провалов 0».

## Данные

На сервере лежит только сгенерированный демонстрационный набор: 105 операций,
сентябрь 2026, все семь сценариев кейса. Настоящих выписок там нет и быть не
должно — машина общая.

Перезалить набор:

```bash
ssh grosh 'cd honest-month && docker compose exec -T app python -m scripts.gen_dataset'
ssh grosh 'cd honest-month && docker compose exec -T app python -m scripts.seed --reset'
ssh grosh 'cd honest-month && docker compose restart'
```

**Последняя строка обязательна.** `seed --reset` пересоздаёт файл базы, а
работающий процесс продолжает держать прежний: SQLite отдаёт открытый
дескриптор, даже когда файл заменён. Без перезапуска экран покажет пустоту при
полной базе — выглядит как поломка, но это не она.

База и загруженные файлы лежат в именованных томах docker и переживают
пересборку образа. Снести данные полностью: `docker compose down -v`.

## Если что-то не работает

| Симптом | Куда смотреть |
|---|---|
| Бот молчит | `docker compose logs --tail 50`; проверить, что `getUpdates` не занят другим запущенным экземпляром — токен один, и два процесса дерутся |
| «API недоступен» в приложении | открыт старый адрес из давнего сообщения бота. Нажать `/start` заново или войти через кнопку меню чата |
| Пусто при полной базе | не сделан `docker compose restart` после `seed --reset` |
| Не разбираются скриншоты и голос | кончились деньги у провайдера ИИ. Всё остальное продолжает работать |
| Тормозит | машина общая, рядом чужие сервисы |
