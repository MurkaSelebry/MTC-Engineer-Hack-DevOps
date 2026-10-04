# Паспорт решения

`Паспорт.pdf` — готовый трехстраничный документ для конкурсного архива. Он собирается из фактической сводки `docs/evidence/summary.json`, версий репозитория и отчетов испытаний. После публикации публичной ветки `main` выполните из корня проекта:

```bash
make package REPO_URL='https://github.com/OWNER/REPOSITORY/tree/main'
```

Замените URL реальным адресом. Упаковщик проверяет анонимную доступность `main`, формат и размер PDF и создает `dist/sel.zip` ровно с двумя файлами: `Ссылка.txt` и `Паспорт.pdf`. Исходники нужно опубликовать в Git, а не добавлять в конкурсный ZIP.

Для изменения и повторной сборки паспорта на Ubuntu:

```bash
sudo apt-get install -y fonts-dejavu-core poppler-utils
python3 -m venv .venv-passport
.venv-passport/bin/pip install -r docs/passport/requirements.txt
.venv-passport/bin/python scripts/build-passport.py
mkdir -p artifacts/passport-preview
pdftoppm -scale-to 1600 -png docs/passport/Паспорт.pdf artifacts/passport-preview/page
pdfinfo docs/passport/Паспорт.pdf
```

Перед сдачей просмотрите все страницы PNG. Генератор проверяет переполнение блоков, наличие отчетов, количество страниц и размер; визуальная проверка нужна дополнительно. На macOS генератор использует системный Arial, на Ubuntu — DejaVu Sans. При смене шрифта документ необходимо повторно отрендерить и проверить.
