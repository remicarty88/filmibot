web: sh -c "gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 16 --worker-class gthread --timeout 300 & python bot.py"
