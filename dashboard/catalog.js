'use strict';

// Один список команд для приложения и режима просмотра в браузере.
const catalog = [
  { id: 'local', title: 'Запустить / перезапустить', detail: 'Поднимет локальное приложение, сохранив данные.', group: 'Локально', command: '.\\scripts\\local.ps1', label: 'Запустить' },
  { id: 'stop', title: 'Остановить локальный API', detail: 'Закроет только локальный сервер на порту 8081.', group: 'Локально', command: '.\\scripts\\local.ps1 -Stop', label: 'Остановить' },
  { id: 'reset', title: 'Сбросить демо-данные', detail: 'Пересоздаст локальную базу. Твои ответы и операции в ней удалятся.', group: 'Локально', command: '.\\scripts\\local.ps1 -Reset', label: 'Сбросить и запустить', confirm: 'Локальная база будет пересоздана. Твои ответы и операции в ней удалятся.' },
  { id: 'tests', title: 'Тесты Python', detail: 'Проверит API, классификацию и расчёты.', group: 'Проверки', command: '.\\.venv\\Scripts\\python.exe -m pytest -q', label: 'Запустить тесты' },
  { id: 'viewport', title: 'Тест Mini App', detail: 'Проверит поведение экрана приложения.', group: 'Проверки', command: 'node --test tests\\miniapp_viewport.test.cjs', label: 'Запустить тест' },
  { id: 'dryrun', title: 'Проверка релиза', detail: 'Соберёт архив и прогонит тесты без отправки на сервер.', group: 'Проверки', command: '.\\deploy\\deploy.ps1 -Ref HEAD -DryRun', label: 'Проверить' },
  { id: 'deploy', title: 'Выложить на сервер', detail: 'Отправит проверенный коммит и перезапустит боевое приложение.', group: 'Сервер', command: 'powershell -NoProfile -ExecutionPolicy Bypass -File .\\deploy\\deploy.ps1 -MaxAttempts 5', label: 'Выложить', confirm: 'Будет запущена выкладка на боевой сервер. Скрипт отправляет только коммит и остановится при незакоммиченных изменениях.' },
  { id: 'ssh', title: 'Подключиться по SSH', detail: 'Откроет интерактивную консоль сервера.', group: 'Сервер', command: 'ssh egor@87.120.84.226', label: 'Открыть консоль' },
];

if (typeof module !== 'undefined' && module.exports) module.exports = catalog;
if (typeof window !== 'undefined') window.GROSH_ACTIONS = catalog;
