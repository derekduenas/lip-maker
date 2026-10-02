FROM python:3.12-slim

WORKDIR /opt/lip-maker
COPY requirements.txt /opt/lip-maker/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /opt/lip-maker

ENV LIP_PAPER=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Paper/demo only. The process refuses a production websocket host.
ENTRYPOINT ["python3", "-m", "mm.unattended"]
CMD ["--run", "--heartbeat", "/var/lib/lip-maker/heartbeat", "--cancel-log", "/var/lib/lip-maker/startup-cancel"]
