# Daily News Digest Workflow

Mistral Workflow: каждый день в 08:00 (Europe/Moscow) ищет свежие новости по двум темам
и отправляет сводку на почту через SMTP Gmail.

Секреты: GMAIL_ADDRESS, GMAIL_APP_PASSWORD.
Workflow: daily-news-digest. Вход: recipient, days, max_items_per_topic, dry_run.
