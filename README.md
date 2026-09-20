# Media Upload API

Минимальный бэкенд для загрузки фотографий и голосовых сообщений.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Ivanaki/CU_Hack)

## Запуск

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

Документация после запуска: http://127.0.0.1:8000/docs

## Загрузка файла

Поле multipart-формы должно называться `file`:

```powershell
curl.exe -X POST http://127.0.0.1:8000/upload -F "file=@photo.jpg"
curl.exe -X POST http://127.0.0.1:8000/upload -F "file=@voice.ogg"
```

Также endpoint принимает файл напрямую в теле запроса — этот режим подходит
для действия «Получить содержимое URL → Тело запроса: Файл» в iOS «Командах».

Файлы сохраняются в папке `uploads/`. Максимальный размер — 20 МБ.

Список загруженных файлов доступен через `GET /files`.

## Публичный адрес

Нажмите **Deploy to Render** выше и подтвердите создание сервиса. После сборки
Render выдаст публичный адрес. Запрос будет выглядеть так:

```powershell
curl.exe -X POST https://ВАШ-СЕРВИС.onrender.com/upload -F "file=@photo.jpg"
```

Для быстрого прототипа файлы хранятся на диске сервиса. На бесплатном сервере
Render это временное хранилище: после перезапуска старые файлы могут исчезнуть.
