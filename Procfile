web: gunicorn pastamaker.wsgi --log-file - --capture-output -k gevent -w 4 --timeout 60
worker: python pastamaker/worker.py
