ЗАПУСК В WINDOWS

Откройте PowerShell и выполняем:

cd "C:\Users\resha\Desktop\garden" python -m venv .venv ..venv\Scripts\python.exe -m pip install -r requirements.txt ..venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000

Открываем в браузере: http://127.0.0.1:8000

Интерактивная документация API: http://127.0.0.1:8000/docs

*Виртуальное окружение активировать не обязательно: команды выше вызывают его Python напрямую. Терминал должен оставаться открытым, пока работает сервер.

Для последующих запусков достаточно:

cd "C:\Users\resha\Desktop\garden" ..venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000

ПЕРВЫЙ ЗАПУСК И ДЕМОНСТРАЦИЯ

При старте приложение создаёт SQLite-базу garden.sqlite3, если её ещё нет. Если соответствующие списки пусты, добавляются демонстрационный проект и workflow Full Security Audit.

    Открываем Dashboard.
    Нажмаем Run audit.
    Ждем статуса success или failed.
    Открываем Security Reports.
    Переключаем All, SECRET, SCA и SAST для фильтрации.
    Нажмимаем Download JSON, чтобы сохранить отображаемый набор находок.
    Открываем Executions для просмотра списка сохранённых запусков.

success означает, что workflow выполнился без ошибки. Это не означает отсутствие уязвимостей.